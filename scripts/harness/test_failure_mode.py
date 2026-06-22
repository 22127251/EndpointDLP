"""Phase 1: orchestrator-side unified per-channel `failure_mode`.

Every orchestrator-side analysis failure — oversize input, analysis timeout, and
analysis error — must resolve to the channel's configured `failure_mode` verdict
(fail_closed → BLOCK, the default; fail_open → ALLOW) instead of a hardcoded
BLOCK. Each test spawns ONE orchestrator and exercises all three channels against
it, asserting the decision follows `failure_mode`.

clipboard and browser read `failure_mode` from their own config section;
peripheral_storage reads it from the nested `transfer_agent` subtree (the
component that owns the verdict for peripheral analysis failures).
"""
from __future__ import annotations

import uuid

import pytest

from pipe_helpers import pipe_send

from orchestrator import messages

_CHANNELS = ("clipboard", "browser", "peripheral_storage")

# config_overrides flipping every channel to fail_open (peripheral via its nested
# transfer_agent subtree). Absent → the orchestrator default of fail_closed.
_FAIL_OPEN = {
    "clipboard": {"failure_mode": "fail_open"},
    "browser": {"failure_mode": "fail_open"},
    "peripheral_storage": {"transfer_agent": {"failure_mode": "fail_open"}},
}


def _send(orch, channel, *, kind="text", text=None, file_path=None, timeout=8.0) -> str:
    payload = {"channel": channel, "kind": kind, "metadata": {}}
    if text is not None:
        payload["text"] = text
    if file_path is not None:
        payload["file_path"] = file_path
    decision, _reason = pipe_send(orch.pipe_name, payload, timeout_seconds=timeout)
    return decision


# ----------------------------- oversize ----------------------------------- #
# A tiny max_clipboard_bytes makes any text request oversize, so the size-cap
# short-circuit in policy_manager fires for every channel (kind=text shares the
# clipboard byte cap). Verdict must follow failure_mode.

@pytest.mark.parametrize("fail_open,expected", [(False, "BLOCK"), (True, "ALLOW")])
def test_oversize_follows_failure_mode(make_orchestrator, fail_open, expected):
    overrides = {"limits": {"max_clipboard_bytes": 4}}
    if fail_open:
        overrides.update(_FAIL_OPEN)
    orch = make_orchestrator(policies_fixture="permissive.yaml", config_overrides=overrides)
    for channel in _CHANNELS:
        decision = _send(orch, channel, text="well over the 4-byte cap")
        assert decision == expected, f"{channel}: expected {expected}, got {decision!r}"


# ------------------------------- error ------------------------------------ #
# A kind=file request whose path does not exist makes policy_manager.analyze
# raise FileNotFoundError (os.path.getsize), which surfaces in the dispatcher's
# generic `except Exception` branch — the analysis-error path. Verdict must
# follow failure_mode.

@pytest.mark.parametrize("fail_open,expected", [(False, "BLOCK"), (True, "ALLOW")])
def test_analysis_error_follows_failure_mode(make_orchestrator, fail_open, expected):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        config_overrides=(_FAIL_OPEN if fail_open else None),
    )
    # Use a SUPPORTED extension (.pdf) so the missing file reaches os.path.getsize
    # → FileNotFoundError → the analysis-error path (a .bin would be short-circuited
    # by the supported-format gate, which is exercised in test_supported_format.py).
    missing = str(orch.tmp_dir / f"does-not-exist-{uuid.uuid4().hex}.pdf")
    for channel in _CHANNELS:
        decision = _send(orch, channel, kind="file", file_path=missing)
        assert decision == expected, f"{channel}: expected {expected}, got {decision!r}"


# ----------------------------- text cap ----------------------------------- #
# A tiny analyzer.max_extracted_chars makes any real file's extracted text exceed
# the cap, so policy_manager raises ExtractionTooLarge during extraction and maps
# it to the channel's failure_mode (reason=text_cap). The temp file is deleted on
# each request (policy_manager finally), so it is rewritten before each channel.

@pytest.mark.parametrize("fail_open,expected", [(False, "BLOCK"), (True, "ALLOW")])
def test_text_cap_follows_failure_mode(make_orchestrator, fail_open, expected):
    overrides = {"analyzer": {"max_extracted_chars": 5}}
    if fail_open:
        overrides.update(_FAIL_OPEN)
    orch = make_orchestrator(policies_fixture="permissive.yaml", config_overrides=overrides)
    over_cap = orch.tmp_dir / "over_cap.txt"
    content = "this text is well over the 5-char extracted cap"
    for channel in _CHANNELS:
        over_cap.write_text(content, encoding="utf-8")
        decision = _send(orch, channel, kind="file", file_path=str(over_cap))
        assert decision == expected, f"{channel}: expected {expected}, got {decision!r}"


# ------------------------------ timeout ----------------------------------- #
# DLP_TEST_SLOW_MS=5000 sleeps every analysis 5 s > the 4 s analysis budget, so
# the dispatcher times out. Verdict must follow failure_mode (was hardcoded
# BLOCK before Phase 1; the fail_closed half overlaps test_timeout.py).

@pytest.mark.slow
@pytest.mark.parametrize("fail_open,expected", [(False, "BLOCK"), (True, "ALLOW")])
def test_timeout_follows_failure_mode(make_orchestrator, fail_open, expected):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        extra_env={"DLP_TEST_SLOW_MS": "5000"},
        config_overrides=(_FAIL_OPEN if fail_open else None),
    )
    for channel in _CHANNELS:
        decision = _send(orch, channel, text="anything", timeout=8.0)
        assert decision == expected, f"{channel}: expected {expected}, got {decision!r}"


# --------------------------- friendly messages ---------------------------- #
# A fail_closed BLOCK now carries the per-category end-user message (the text the
# browser popup / clipboard / Transfer Agent Note shows), not a policy id or a
# stale "Analysis timed out". Verify each category's message reaches the client.

def test_oversize_block_carries_friendly_message(make_orchestrator):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        config_overrides={"limits": {"max_clipboard_bytes": 4}},
    )
    decision, reason = pipe_send(
        orch.pipe_name, {"channel": "browser", "kind": "text",
                         "text": "well over the 4-byte cap", "metadata": {}},
        timeout_seconds=8.0)
    assert decision == "BLOCK"
    assert reason == messages.FAILURE_MESSAGES["oversize"]


def test_analysis_error_block_carries_friendly_message(make_orchestrator):
    orch = make_orchestrator(policies_fixture="permissive.yaml")
    missing = str(orch.tmp_dir / f"missing-{uuid.uuid4().hex}.pdf")
    decision, reason = pipe_send(
        orch.pipe_name, {"channel": "browser", "kind": "file",
                         "file_path": missing, "metadata": {}},
        timeout_seconds=8.0)
    assert decision == "BLOCK"
    assert reason == messages.FAILURE_MESSAGES["analysis_error"]
