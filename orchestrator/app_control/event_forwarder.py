"""Forward our policy's App Control block events to ``events.jsonl`` (Phase AC-3).

Push-callback subscription (``win32evtlog.EvtSubscribe``) modelled on the AC-1
keeper ``scripts/spike-evt-subscribe.py`` (proven as SYSTEM on the VM). The primary
feed is ``Microsoft-Windows-CodeIntegrity/Operational`` — **3077** (enforce block)
and **3076** (audit block); ``Microsoft-Windows-AppLocker/MSI and Script`` is
subscribed as cheap insurance (AC-1 showed MSIX blocks actually surface as 3077 in
the CI channel, carrying ``PackageFamilyName``).

Every event is filtered to **our** policy by the ``PolicyGUID`` data field (braced,
lowercase — pinned in ``spike-results/RESULTS.md``; note the ``PolicyID`` data
field is the policy's date-stamped ``Settings\\Id`` string, NOT the GUID). The
callback swallows every exception — nothing may propagate into the pywin32 thunk.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Callable

from orchestrator.events import record_app_control_event

log = logging.getLogger("orchestrator.app_control.event_forwarder")

EVENT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"

CI_CHANNEL = "Microsoft-Windows-CodeIntegrity/Operational"
APPLOCKER_CHANNEL = "Microsoft-Windows-AppLocker/MSI and Script"

#: CodeIntegrity block events we forward → audit outcome. (AppLocker events carry
#: no PolicyGUID, so they cannot be attributed to our policy and are dropped at the
#: filter; the subscription is insurance only.)
_OUTCOME = {3077: "blocked", 3076: "audit"}

_DEFAULT_CHANNELS = {
    CI_CHANNEL: (3076, 3077),
    APPLOCKER_CHANNEL: (8028, 8029, 8039, 8040),
}


def _bare_guid(guid: str) -> str:
    return guid.strip().strip("{}").lower()


def build_xpath(event_ids) -> str:
    if not event_ids:
        return "*"
    clauses = " or ".join("EventID=%d" % e for e in event_ids)
    return "*[System[(%s)]]" % clauses


def parse_block_event(xml_text: str, our_policy_bare: str):
    """Pure: parse a rendered event. Return ``(outcome, detail)`` if it is a CI
    block (3076/3077) for **our** PolicyGUID, else ``None``. Never raises."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    system = root.find(EVENT_NS + "System")
    if system is None:
        return None
    try:
        eid = int(system.findtext(EVENT_NS + "EventID", "").strip())
    except (TypeError, ValueError):
        return None
    if eid not in _OUTCOME:
        return None

    data: dict[str, str] = {}
    ed = root.find(EVENT_NS + "EventData")
    if ed is not None:
        for d in ed.findall(EVENT_NS + "Data"):
            name = d.get("Name")
            if name:
                data[name] = d.text or ""

    if _bare_guid(data.get("PolicyGUID", "")) != our_policy_bare:
        return None  # foreign policy (or no GUID) — decision 9: only our blocks

    detail = {
        "event_id": eid,
        "file": data.get("File Name"),
        "process": data.get("Process Name"),
        "policy_guid": data.get("PolicyGUID"),
        "policy_name": data.get("PolicyName"),
        "internal_name": data.get("InternalName"),
        "original_file_name": data.get("OriginalFileName"),
        "product_name": data.get("ProductName"),
        "file_version": data.get("FileVersion"),
        "package_family_name": data.get("PackageFamilyName"),
        "signing_scenario": data.get("SI Signing Scenario"),
        "sha256_flat": data.get("SHA256 Flat Hash"),
        "record_id": (system.findtext(EVENT_NS + "EventRecordID", "") or "").strip() or None,
    }
    detail = {k: v for k, v in detail.items() if v not in (None, "")}
    return _OUTCOME[eid], detail


class EventForwarder:
    def __init__(self, *, policy_id: str,
                 on_block: Callable[[str, dict], None] | None = None,
                 channels: dict | None = None) -> None:
        self._policy_bare = _bare_guid(policy_id)
        self._on_block = on_block
        self._channels = channels or _DEFAULT_CHANNELS
        self._handles: list = []
        self._win32 = None

    def start(self) -> bool:
        """Subscribe to each channel. Best-effort: a failed subscription is logged
        and skipped (the channel keeps running). Returns True if any subscription
        was established."""
        try:
            import win32evtlog  # lazy: Windows-only; present in the embed
        except ImportError as exc:
            log.warning("win32evtlog unavailable; block forwarding disabled: %s", exc)
            return False
        self._win32 = win32evtlog
        for channel, ids in self._channels.items():
            try:
                handle = win32evtlog.EvtSubscribe(
                    channel,
                    win32evtlog.EvtSubscribeToFutureEvents,
                    None,                       # SignalEvent (unused: push model)
                    Callback=self._on_event,
                    Context=channel,
                    Query=build_xpath(ids),
                )
                self._handles.append(handle)
                log.info("Subscribed to %s", channel)
            except Exception as exc:  # noqa: BLE001 — access denied / unknown channel
                log.warning("Could not subscribe to %s: %s", channel, exc)
        return bool(self._handles)

    def _on_event(self, action, context, handle) -> None:
        # MUST swallow everything — exceptions cannot cross back into pywin32.
        try:
            if action == self._win32.EvtSubscribeActionError:
                return
            xml_text = self._win32.EvtRender(handle, self._win32.EvtRenderEventXml)
            result = parse_block_event(xml_text, self._policy_bare)
            if result is None:
                return
            outcome, detail = result
            record_app_control_event(event="block", outcome=outcome, detail=detail)
            if self._on_block is not None:
                try:
                    self._on_block(outcome, detail)
                except Exception:  # noqa: BLE001
                    log.exception("on_block callback failed")
        except Exception:  # noqa: BLE001
            log.exception("event callback failed")

    def stop(self) -> None:
        # Dropping the subscription handles ends delivery (pywin32 auto-closes them).
        self._handles = []

    @property
    def running(self) -> bool:
        return bool(self._handles)
