"""Gap 3: dispatcher timeout path under the default (fail_closed) failure_mode.

When analysis exceeds the orchestrator's `_ANALYSIS_TIMEOUT` (4 s), the
dispatcher returns the channel's failure_mode verdict. With the default
fail_closed that is BLOCK + the per-category "timeout" user message, which these
tests pin (including the user-facing reason string). The fail_open counterpart —
and the oversize/error failure paths — live in test_failure_mode.py. We force the
slow path via the DLP_TEST_SLOW_MS env var (5 s sleep) so the test is
deterministic instead of dependent on extract_text timing.
"""
from __future__ import annotations

import time

import pytest

from pipe_helpers import pipe_send

from orchestrator import messages


@pytest.mark.slow
def test_browser_analysis_timeout_blocks(make_orchestrator):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        pool_overrides={"browser_workers": 1},
        extra_env={"DLP_TEST_SLOW_MS": "5000"},  # 5 s > 4 s analysis cap → timeout
    )

    start = time.monotonic()
    decision, reason = pipe_send(
        orch.pipe_name,
        {"channel": "browser", "kind": "text", "text": "anything", "metadata": {}},
        timeout_seconds=8.0,
    )
    elapsed = time.monotonic() - start

    assert decision == "BLOCK", f"expected BLOCK on timeout, got {decision!r}"
    assert reason == messages.FAILURE_MESSAGES["timeout"], f"expected timeout reason, got {reason!r}"
    # Server-side budget is 4 s; allow generous margin for spawn/spurious latency.
    assert elapsed < 6.0, f"timeout took too long: {elapsed:.2f}s"


@pytest.mark.slow
def test_peripheral_analysis_timeout_blocks(make_orchestrator):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        pool_overrides={"peripheral_storage_workers": 1},
        extra_env={"DLP_TEST_SLOW_MS": "5000"},
    )

    start = time.monotonic()
    decision, _reason = pipe_send(
        orch.pipe_name,
        {"channel": "peripheral_storage", "kind": "text", "text": "x", "metadata": {}},
        timeout_seconds=8.0,
    )
    elapsed = time.monotonic() - start

    assert decision == "BLOCK"
    assert elapsed < 6.0, f"timeout took too long: {elapsed:.2f}s"
