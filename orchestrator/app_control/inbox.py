"""Inbox watcher — poll the drop-folder, validate a push, hand it to the deployer
(Phase AC-3).

A push is a **subfolder** under ``inbox\\`` containing ``{policy.xml, {PolicyID}.cip,
manifest.json}`` (the manifest is written/moved **last**). The watcher polls every
``poll_seconds`` (poll, not watchdog — robust to a non-atomic copy, e.g. across a
VMware shared folder). A push is picked up only when its ``manifest.json`` exists
**and** the subfolder's file sizes are **stable across two consecutive polls**
(tolerates an in-progress copy).

On pickup it runs the AC-2 manifest validator suite (``manifest.validate_all`` +
a single-PolicyID guard). A clean push goes to ``Deployer.deploy``; a failing push
(or a failed deploy) is moved to ``rejected\\`` with an audit record. The whole
loop is fail-safe: one bad push can never crash the thread, and nothing is
deployed on doubt.

Event ownership: the **deployer** emits deploy/remove/neutralize lifecycle events;
the **watcher** emits ``reject`` events for validation failures.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.events import record_app_control_event

from . import manifest as mf

log = logging.getLogger("orchestrator.app_control.inbox")


def _bare_guid(guid: str) -> str:
    return guid.strip().strip("{}").lower()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


class InboxWatcher:
    def __init__(self, *, inbox_dir: str | Path, rejected_dir: str | Path,
                 deployer, base_policy_id: str, install_root: str | Path,
                 dotnet_root: str | Path | None = None,
                 extra_paths: list | None = None, poll_seconds: float = 3.0,
                 reconcile_interval_seconds: float = 30.0) -> None:
        self._inbox = Path(inbox_dir)
        self._rejected = Path(rejected_dir)
        self._deployer = deployer
        self._base_policy_id = base_policy_id
        self._install_root = install_root
        self._dotnet_root = dotnet_root
        self._extra_paths = extra_paths
        self._poll_seconds = poll_seconds
        self._reconcile_interval = reconcile_interval_seconds
        self._last_reconcile = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._seen: dict[str, dict[str, int]] = {}  # subfolder -> {filename: size}
        self._rejected_count = 0

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        log.info("Inbox watcher polling %s every %ss", self._inbox, self._poll_seconds)
        self._maybe_reconcile(force=True)   # self-heal the record on (re)start
        while not self._stop.is_set():
            try:
                self.poll_once()
                self._maybe_reconcile()
            except Exception:  # noqa: BLE001 — never let the loop die
                log.exception("inbox poll failed")
            self._stop.wait(self._poll_seconds)
        log.info("Inbox watcher stopped.")

    def _maybe_reconcile(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_reconcile) < self._reconcile_interval:
            return
        self._last_reconcile = now
        try:
            self._deployer.reconcile()
        except Exception:  # noqa: BLE001 — reconcile must never break the loop
            log.exception("reconcile failed")

    def stop(self) -> None:
        self._stop.set()

    def set_poll_seconds(self, value: float) -> None:
        """Bounded hot-reload of the poll interval (channel.apply_config)."""
        self._poll_seconds = value

    @property
    def rejected_count(self) -> int:
        return self._rejected_count

    def pending_count(self) -> int:
        try:
            return sum(1 for d in self._inbox.iterdir() if d.is_dir())
        except OSError:
            return 0

    # -- core --------------------------------------------------------------

    def poll_once(self) -> None:
        """One scan: find ready push subfolders (manifest present + size-stable) and
        process each. Tracks per-folder size snapshots across calls for stability."""
        if not self._inbox.is_dir():
            return
        current = {d.name for d in self._inbox.iterdir() if d.is_dir()}
        # forget folders that disappeared (consumed / moved)
        for gone in [k for k in self._seen if k not in current]:
            self._seen.pop(gone, None)

        for sub in sorted(d for d in self._inbox.iterdir() if d.is_dir()):
            try:
                if not (sub / "manifest.json").is_file():
                    continue  # manifest written last → not ready
                snapshot = {f.name: f.stat().st_size
                            for f in sub.iterdir() if f.is_file()}
                prev = self._seen.get(sub.name)
                self._seen[sub.name] = snapshot
                if prev != snapshot:
                    continue  # still settling — wait one more poll
                self._seen.pop(sub.name, None)
                self._process(sub)
            except Exception:  # noqa: BLE001 — one bad push can't stop the scan
                log.exception("processing push %s failed", sub)

    def _process(self, sub: Path) -> None:
        with self._lock:
            # 1) parse manifest
            try:
                m = mf.parse_manifest((sub / "manifest.json").read_text(encoding="utf-8"))
            except (mf.ManifestError, OSError) as exc:
                self._reject(sub, [mf.Failure("manifest_parse", str(exc))])
                return
            # 2) single-PolicyID guard — the channel manages exactly our base GUID
            if _bare_guid(m.policy_id) != _bare_guid(self._base_policy_id):
                self._reject(sub, [mf.Failure(
                    "foreign_policy_id",
                    f"push PolicyID {m.policy_id} != managed {self._base_policy_id}")])
                return
            # 3) AC-2 validator suite (hashes, cip-name, version, self-protect)
            failures = mf.validate_all(
                m, sub,
                deployed_version_ex=self._deployer.deployed_version_ex(),
                install_root=self._install_root,
                dotnet_root=self._dotnet_root,
                extra_paths=self._extra_paths,
            )
            if failures:
                self._reject(sub, failures)
                return
            # 4) deploy (the deployer emits the deploy/ok|failed audit line)
            if self._deployer.deploy(sub, m):
                self._consume(sub)
            else:
                # already audited as deploy/failed by the deployer; quarantine so it
                # isn't retried in a loop.
                self._move_to_rejected(sub)

    def _reject(self, sub: Path, failures: list) -> None:
        self._move_to_rejected(sub)
        self._rejected_count += 1
        record_app_control_event(
            event="reject", outcome="rejected",
            detail={"folder": sub.name,
                    "failures": [{"code": f.code, "detail": f.detail} for f in failures]})
        log.warning("rejected push %s: %s", sub.name,
                    ", ".join(f.code for f in failures))

    def _move_to_rejected(self, sub: Path) -> None:
        try:
            self._rejected.mkdir(parents=True, exist_ok=True)
            dest = self._rejected / f"{_utc_stamp()}_{sub.name}"
            shutil.move(str(sub), str(dest))
        except OSError:
            log.exception("could not move %s to rejected; deleting", sub)
            shutil.rmtree(sub, ignore_errors=True)

    def _consume(self, sub: Path) -> None:
        shutil.rmtree(sub, ignore_errors=True)
