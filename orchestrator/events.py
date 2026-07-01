"""Phase F structured decision log — one JSON line per delivered DLP decision.

Writes to the ``dlp.events`` logger, which ``logging_setup.configure_logging``
points at ``%PROGRAMDATA%\\DLP\\logs\\events.jsonl`` (rotating, propagate=False).
The dispatcher calls :func:`record_decision` exactly once per request, at the
single point where every channel's decision converges.
"""
from __future__ import annotations

import datetime
import json
import logging
import urllib.parse

_log = logging.getLogger("dlp.events")


def _clean_url(raw: str) -> str:
    """Keep scheme://host/path; drop the query string + fragment.

    Upload endpoints (e.g. Google Drive) carry enormous query strings that
    flood the log without adding audit value — the host+path identify the
    destination. Returns the raw string unchanged if it can't be parsed.
    """
    try:
        parts = urllib.parse.urlsplit(raw)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return raw


def record_decision(
    *,
    channel: str,
    kind: str,
    name: str | None,
    url: str | None,
    decision: str,
    violations: list[dict],
    elapsed_ms: float,
    req_id: str,
    superseded: bool = False,
    reason: str | None = None,
) -> None:
    """Emit one audit line. ``violations`` is a list of
    ``{"policy_id","action","count","with_context","context_words_triggered"}`` objects
    — count = matches for that policy, action = the strongest action it resolved to,
    with_context = how many matches had a confidence-boosting context word, and
    context_words_triggered = the distinct context words that triggered those boosts
    (generic terms only — never the matched value; NOT the policy's full context list).

    ``reason`` is the stable machine category behind a BLOCK — ``policy_violation``
    for a real policy hit, or one of ``oversize`` / ``text_cap`` /
    ``unsupported_format`` / ``timeout`` / ``analysis_error`` / ``malformed`` for a
    failure-mode block. It mirrors Elastic ECS ``event.reason`` / OCSF
    ``status_detail`` so a reader can tell WHY a request was blocked (an
    empty-``violations`` BLOCK was previously ambiguous). Omitted on ALLOW."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    rec: dict = {
        "ts": ts,
        "req_id": req_id,
        "channel": channel,
        "kind": kind,
        "decision": decision,
        "violations": violations,
        "elapsed_ms": round(elapsed_ms, 1),
        "superseded": superseded,
    }
    if reason:
        rec["reason"] = reason
    if name:
        rec["name"] = name
    if url:
        rec["url"] = _clean_url(url)
    # ensure_ascii=False so Vietnamese filenames/URLs stay readable in the log.
    _log.info(json.dumps(rec, ensure_ascii=False))


def record_app_control_event(*, event: str, outcome: str,
                             detail: dict | None = None) -> None:
    """Phase AC-3 — emit one App Control (WDAC) audit line to ``events.jsonl``.

    Shares the ``dlp.events`` logger with :func:`record_decision` but carries an
    app-control-shaped payload (``record_decision``'s content-analysis signature
    doesn't fit policy deploy/remove/block records). ``event`` is the operation
    (``"deploy"`` / ``"reject"`` / ``"remove"`` / ``"neutralize"`` / ``"block"`` /
    ``"error"``), ``outcome`` its result (``"ok"`` / ``"rejected"`` / ``"failed"``
    / ``"blocked"`` / ``"audit"``), and ``detail`` any structured context
    (file/process/policy_guid for blocks; failures for rejects).
    """
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    rec: dict = {
        "ts": ts,
        "channel": "app_control",
        "event": event,
        "outcome": outcome,
    }
    if detail:
        rec.update(detail)
    _log.info(json.dumps(rec, ensure_ascii=False))
