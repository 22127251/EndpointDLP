"""Gap 2: policy hot-reload under load (strict bar).

After `policies.yaml` is atomically replaced, every NEW request must use the
new engine. We confirm by polling until a known visa-number payload starts
returning BLOCK (proves swap completed), then send a batch of fresh requests
and assert all of them BLOCK.
"""
from __future__ import annotations

import time

from pipe_helpers import pipe_send

_VISA = "4111 1111 1111 1111"


def _send_visa(pipe_name: str, timeout: float = 4.0) -> tuple[str, str]:
    return pipe_send(
        pipe_name,
        {"channel": "browser", "kind": "text", "text": _VISA, "metadata": {}},
        timeout,
    )


def _read_fixture(name: str) -> str:
    from pathlib import Path
    return (Path(__file__).parent / "fixture_policies" / name).read_text(encoding="utf-8")


def test_strict_hot_reload_atomic_save(make_orchestrator):
    orch = make_orchestrator(policies_fixture="permissive.yaml")

    # Baseline: visa number ALLOWs under permissive policy.
    decision, _ = _send_visa(orch.pipe_name)
    assert decision == "ALLOW", f"baseline should ALLOW, got {decision}"

    # Atomically swap to the visa-blocking policy.
    orch.write_policies(_read_fixture("visa_block.yaml"), atomic=True)

    # Poll until first BLOCK observed (swap confirmed). Allow 2 s budget.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        decision, reason = _send_visa(orch.pipe_name)
        if decision == "BLOCK":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("policy reload did not take effect within 2 s")

    # Strict bar: every request AFTER the swap must BLOCK.
    for i in range(10):
        d, r = _send_visa(orch.pipe_name)
        assert d == "BLOCK", f"strict-bar violation at request {i}: got {d!r} reason={r!r}"


def test_strict_hot_reload_in_place_write(make_orchestrator):
    # Same guarantee for non-atomic (truncate-and-rewrite) saves.
    orch = make_orchestrator(policies_fixture="permissive.yaml")

    decision, _ = _send_visa(orch.pipe_name)
    assert decision == "ALLOW"

    orch.write_policies(_read_fixture("visa_block.yaml"), atomic=False)

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        decision, _ = _send_visa(orch.pipe_name)
        if decision == "BLOCK":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("in-place reload did not take effect within 2 s")

    for i in range(10):
        d, r = _send_visa(orch.pipe_name)
        assert d == "BLOCK", f"strict-bar violation at request {i}: got {d!r}"
