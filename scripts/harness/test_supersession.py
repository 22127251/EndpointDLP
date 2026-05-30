"""Gap 4: clipboard supersession edge cases.

With `clipboard_workers=1` and a slow analysis hook, three rapidly-fired
clipboard requests should result in exactly ONE real response (the request that
was not superseded by a later one) and TWO clients that observe the pipe being
closed without a response (raising an exception).
"""
from __future__ import annotations

import threading
import time

import pywintypes
import pytest

from pipe_helpers import pipe_send


def _send_one(pipe_name: str, idx: int, results: dict, errors: dict) -> None:
    payload = {
        "channel": "clipboard",
        "kind": "text",
        "text": f"clip-{idx}",
        "metadata": {},
    }
    try:
        results[idx] = pipe_send(pipe_name, payload, timeout_seconds=8.0)
    except (pywintypes.error, OSError, ValueError, TimeoutError) as exc:
        errors[idx] = exc


def test_clipboard_supersession_drops_old_responses(make_orchestrator):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        pool_overrides={"clipboard_workers": 1},
        extra_env={"DLP_TEST_SLOW_MS": "500"},  # each analysis takes ~0.5 s
    )

    n = 3
    results: dict[int, tuple[str, str]] = {}
    errors: dict[int, Exception] = {}
    threads = []
    for i in range(n):
        t = threading.Thread(
            target=_send_one,
            args=(orch.pipe_name, i, results, errors),
        )
        threads.append(t)
        t.start()
        # Brief offset so the orchestrator clearly sees ordered arrivals.
        time.sleep(0.02)

    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "supersession thread hung"

    # Exactly one client should observe a real ALLOW/BLOCK response; the others
    # should have raised because the orchestrator dropped (suppressed-write) the
    # superseded responses.
    assert len(results) == 1, (
        f"expected exactly 1 success, got {len(results)}: results={results} errors={errors}"
    )
    decision, _ = next(iter(results.values()))
    assert decision == "ALLOW", f"the surviving request should ALLOW under permissive, got {decision}"
    assert len(errors) == n - 1, f"expected {n-1} errored clients, got {len(errors)}: {errors}"
