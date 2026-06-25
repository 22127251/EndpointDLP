"""The clipboard channel carries large inline text, governed by max_extracted_chars.

The orchestrator's data-pipe is MESSAGE-mode; the server reassembles the whole
message (server.py:_read_message), bounded by a memory ceiling derived from
analyzer.max_extracted_chars. The ClipboardInterceptor sends the FULL copied text
(no client-side byte cap); the analyzer decides whether to scan it based on
analyzer.max_extracted_chars.

These tests prove, end-to-end through a real orchestrator subprocess:
  - a >64 KB and an ~8 MB clipboard text (both under the char cap) are read in
    full and analyzed (ALLOW),
  - PII buried in the middle of an ~8 MB body is still detected (the WHOLE body
    is scanned, not just the first 64 KB),
  - text OVER analyzer.max_extracted_chars is refused without scanning and fails
    per the channel's failure_mode (reason=text_cap).
"""
from __future__ import annotations

import pytest

from pipe_helpers import pipe_send

# Char cap high enough for the 8 MB ('a'*8M = 8M chars) bodies, plus a generous
# analysis budget so the test is not timing-sensitive (the harness default is 4 s)
# and the client waits long enough for the round-trip.
_BIG_CAP = {
    "analyzer": {"max_extracted_chars": 16_000_000},
    "service": {"analysis_timeout_seconds": 20},
}
_PIPE_TIMEOUT = 30.0


def _send_text(orch, text, timeout=_PIPE_TIMEOUT):
    payload = {"channel": "clipboard", "kind": "text", "text": text, "metadata": {}}
    return pipe_send(orch.pipe_name, payload, timeout_seconds=timeout)


@pytest.mark.parametrize("size", [100 * 1024, 8 * 1024 * 1024])
def test_large_clean_text_allowed(make_orchestrator, size):
    """>64 KB and ~8 MB clean clipboard text (under the char cap) are
    reassembled and ALLOWed."""
    orch = make_orchestrator(policies_fixture="permissive.yaml", config_overrides=_BIG_CAP)
    decision, reason = _send_text(orch, "a" * size)
    assert decision == "ALLOW", f"size={size}: {reason}"


def test_large_text_with_pii_blocks(make_orchestrator):
    """A Visa number buried in the middle of an ~8 MB body is still detected —
    proves the whole message reached the analyzer, not just the first 64 KB.
    The body stays well under max_extracted_chars so the BLOCK is on PII, not size."""
    orch = make_orchestrator(policies_fixture="visa_block.yaml", config_overrides=_BIG_CAP)
    half = "a" * (4 * 1024 * 1024)          # ~8.4 MB total, < the 16M char cap
    body = half + " 4111 1111 1111 1111 " + half
    decision, reason = _send_text(orch, body)
    assert decision == "BLOCK", reason


@pytest.mark.parametrize("fail_open,expected", [(False, "BLOCK"), (True, "ALLOW")])
def test_over_cap_follows_failure_mode(make_orchestrator, fail_open, expected):
    """Clipboard text longer than analyzer.max_extracted_chars is refused without
    scanning (reason=text_cap) and follows the channel's failure_mode."""
    overrides = {"analyzer": {"max_extracted_chars": 100_000}}
    if fail_open:
        overrides["clipboard"] = {"failure_mode": "fail_open"}
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        config_overrides=overrides,
    )
    decision, _ = _send_text(orch, "a" * 200_000)   # 200K chars > 100K cap
    assert decision == expected
