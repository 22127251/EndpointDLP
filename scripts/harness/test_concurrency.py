"""Gap 1: multi-instance pipe concurrency.

Fires N parallel pipe clients at an orchestrator with pipe_listeners=4 and
permissive policies. All should return ALLOW within the deadline; none should
deadlock or fail.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid

from pipe_helpers import pipe_send


def test_parallel_browser_requests_all_allow(make_orchestrator):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        pool_overrides={"pipe_listeners": 4, "browser_workers": 3},
    )

    n_clients = 16
    payloads = [
        {
            "channel": "browser",
            "kind": "text",
            "text": f"hello-{uuid.uuid4().hex}",
            "metadata": {},
        }
        for _ in range(n_clients)
    ]

    results: list = []
    with ThreadPoolExecutor(max_workers=n_clients) as pool:
        futures = [pool.submit(pipe_send, orch.pipe_name, p, 8.0) for p in payloads]
        for fut in as_completed(futures, timeout=15):
            results.append(fut.result())

    assert len(results) == n_clients
    for decision, reason in results:
        assert decision == "ALLOW", f"expected ALLOW, got {decision!r} reason={reason!r}"


def test_parallel_peripheral_requests_all_allow(make_orchestrator):
    # Peripheral_storage channel has no supersession; every request gets a real
    # decision. Validates the dispatcher's peripheral pool concurrently with
    # pipe accept threads.
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        pool_overrides={"peripheral_storage_workers": 2, "pipe_listeners": 4},
    )

    n_clients = 8
    payloads = [
        {
            "channel": "peripheral_storage",
            "kind": "text",
            "text": f"periph-{uuid.uuid4().hex}",
            "metadata": {},
        }
        for _ in range(n_clients)
    ]

    with ThreadPoolExecutor(max_workers=n_clients) as pool:
        futures = [pool.submit(pipe_send, orch.pipe_name, p, 8.0) for p in payloads]
        results = [f.result(timeout=12) for f in futures]

    for decision, reason in results:
        assert decision == "ALLOW", f"got {decision!r} reason={reason!r}"
