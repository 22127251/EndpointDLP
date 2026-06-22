"""Phase 7: the clipboard channel carries large inline text.

The orchestrator's data-pipe is MESSAGE-mode; before Phase 7 the server did a
single 64 KB ReadFile, so any clipboard text over ~64 KB was truncated → JSON
parse failed → no response → the client failed closed. Phase 7 reassembles the
whole message (server.py:_read_message), gated only by clipboard.max_input_bytes.

These tests prove, end-to-end through a real orchestrator subprocess:
  - a >64 KB and an ~8 MB clipboard text are read in full and analyzed (ALLOW),
  - PII buried in the middle of an ~8 MB body is still detected (the WHOLE body
    is scanned, not just the first 64 KB),
  - text over clipboard.max_input_bytes fails per the channel's failure_mode
    (the new key path; reason=size_limit).
"""
from __future__ import annotations

import pytest

from pipe_helpers import pipe_send

# Cap high enough for the 8 MB bodies, plus a generous analysis budget so the
# test is not timing-sensitive (the harness default is 4 s) and the client waits
# long enough for the round-trip.
_BIG_CAP = {
    "clipboard": {"max_input_bytes": 16_000_000},
    "service": {"analysis_timeout_seconds": 20},
}
_PIPE_TIMEOUT = 30.0


def _send_text(orch, text, timeout=_PIPE_TIMEOUT):
    payload = {"channel": "clipboard", "kind": "text", "text": text, "metadata": {}}
    return pipe_send(orch.pipe_name, payload, timeout_seconds=timeout)


@pytest.mark.parametrize("size", [100 * 1024, 8 * 1024 * 1024])
def test_large_clean_text_allowed(make_orchestrator, size):
    """>64 KB and ~8 MB clean clipboard text are reassembled and ALLOWed."""
    orch = make_orchestrator(policies_fixture="permissive.yaml", config_overrides=_BIG_CAP)
    decision, reason = _send_text(orch, "a" * size)
    assert decision == "ALLOW", f"size={size}: {reason}"


def test_large_text_with_pii_blocks(make_orchestrator):
    """A Visa number buried in the middle of an ~8 MB body is still detected —
    proves the whole message reached the analyzer, not just the first 64 KB.
    The body stays well under max_input_bytes so the BLOCK is on PII, not size."""
    orch = make_orchestrator(policies_fixture="visa_block.yaml", config_overrides=_BIG_CAP)
    half = "a" * (4 * 1024 * 1024)          # ~8.4 MB total, < the 16 MB cap
    body = half + " 4111 1111 1111 1111 " + half
    decision, reason = _send_text(orch, body)
    assert decision == "BLOCK", reason


@pytest.mark.parametrize("fail_open,expected", [(False, "BLOCK"), (True, "ALLOW")])
def test_over_cap_follows_failure_mode(make_orchestrator, fail_open, expected):
    """Text over clipboard.max_input_bytes fails per the channel's failure_mode
    (exercises the new clipboard.max_input_bytes key; reason=size_limit)."""
    clip = {"max_input_bytes": 100_000}
    if fail_open:
        clip["failure_mode"] = "fail_open"
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        config_overrides={"clipboard": clip},
    )
    decision, _ = _send_text(orch, "a" * 200_000)
    assert decision == expected
