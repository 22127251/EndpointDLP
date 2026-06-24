"""Layer B — end-to-end config hot-reload through a real orchestrator subprocess.

Proves the whole wire (save config.yaml -> ConfigWatcher -> _handle_config_change
-> OrchestratorConfig.apply_hot_reload -> live PolicyManager/Dispatcher) actually
changes the agent's behavior for every server-side hot-reloadable field that has an
observable pipe verdict, AND that a restart-only field (data_pipe) does NOT take
effect on reload. (drain_timeout_seconds has no steady-state behavior — it is only
observed at shutdown — so it is covered by Layer A introspection, not here.)

Mirrors test_hot_reload.py (policy reload): rewrite the watched config.yaml, then
poll pipe_send until the new behavior appears.
"""
from __future__ import annotations

import os
import time

import pytest
import yaml

from pipe_helpers import pipe_send

_POLL_BUDGET_S = 6.0


def _deep_merge(base: dict, overrides: dict) -> None:
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _rewrite_config(orch, overrides: dict) -> None:
    """Deep-merge *overrides* into the orchestrator's watched config.yaml and
    atomically replace it (write-temp + os.replace), which the ConfigWatcher
    applies the same way an editor's atomic save does."""
    raw = yaml.safe_load(orch.config_path.read_text(encoding="utf-8"))
    _deep_merge(raw, overrides)
    tmp = orch.config_path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    os.replace(tmp, orch.config_path)


def _decision_text(orch, text, channel="clipboard", timeout=_POLL_BUDGET_S) -> str:
    payload = {"channel": channel, "kind": "text", "text": text, "metadata": {}}
    return pipe_send(orch.pipe_name, payload, timeout_seconds=timeout)[0]


def _decision_file(orch, path, channel="browser", timeout=_POLL_BUDGET_S) -> str:
    payload = {"channel": channel, "kind": "file", "file_path": str(path), "metadata": {}}
    return pipe_send(orch.pipe_name, payload, timeout_seconds=timeout)[0]


def _poll_until(send_fn, expected, budget=_POLL_BUDGET_S) -> None:
    """Call send_fn() until it returns *expected* (the reload landed) or the budget
    elapses. send_fn must be idempotent (re-create any consumed temp file itself)."""
    deadline = time.monotonic() + budget
    last = None
    while time.monotonic() < deadline:
        last = send_fn()
        if last == expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"behavior did not become {expected} within {budget}s (last={last!r})")


# ── failure_mode (per channel), via the clipboard/browser text_cap path ──────
_FM_OVERRIDE = {
    "clipboard": {"clipboard": {"failure_mode": "fail_open"}},
    "browser": {"browser": {"failure_mode": "fail_open"}},
    "peripheral_storage": {"peripheral_storage": {"transfer_agent": {"failure_mode": "fail_open"}}},
}


@pytest.mark.parametrize("channel", ["clipboard", "browser", "peripheral_storage"])
def test_failure_mode_reloads(make_orchestrator, channel):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        config_overrides={"analyzer": {"max_extracted_chars": 50}},
    )
    over_cap = "x" * 100   # > the 50-char cap → text_cap → failure_mode verdict
    assert _decision_text(orch, over_cap, channel=channel) == "BLOCK"   # fail_closed default
    _rewrite_config(orch, _FM_OVERRIDE[channel])
    _poll_until(lambda: _decision_text(orch, over_cap, channel=channel), "ALLOW")


def test_max_extracted_chars_reloads(make_orchestrator):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        config_overrides={"analyzer": {"max_extracted_chars": 50},
                          "clipboard": {"failure_mode": "fail_closed"}},
    )
    text = "y" * 100
    assert _decision_text(orch, text) == "BLOCK"          # 100 > 50 → text_cap → BLOCK
    _rewrite_config(orch, {"analyzer": {"max_extracted_chars": 100000}})
    _poll_until(lambda: _decision_text(orch, text), "ALLOW")   # now under cap → scanned, clean


def test_max_file_bytes_reloads(make_orchestrator):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        config_overrides={"limits": {"max_file_bytes": 4}},
    )
    f = orch.tmp_dir / "probe.txt"

    def _send():
        f.write_text("clean content well over four bytes", encoding="utf-8")
        return _decision_file(orch, f)

    assert _send() == "BLOCK"                              # 34 bytes > 4 → oversize → BLOCK
    _rewrite_config(orch, {"limits": {"max_file_bytes": 10_000_000}})
    _poll_until(_send, "ALLOW")                            # now under cap → scanned, clean


def test_supported_extensions_reloads(make_orchestrator):
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        config_overrides={"analyzer": {"supported_extensions": [".txt", ".pdf"]}},
    )
    f = orch.tmp_dir / "probe.xyz"

    def _send():
        f.write_text("clean content", encoding="utf-8")
        return _decision_file(orch, f)

    assert _send() == "BLOCK"                              # .xyz not supported → unsupported_format
    _rewrite_config(orch, {"analyzer": {"supported_extensions": [".txt", ".pdf", ".xyz"]}})
    _poll_until(_send, "ALLOW")                            # .xyz now scanned as plaintext → clean


@pytest.mark.slow
def test_analysis_timeout_seconds_reloads(make_orchestrator):
    # DLP_TEST_SLOW_MS=400 sleeps every analysis 400 ms. With a 0.1 s budget the
    # dispatcher times out (BLOCK); raising the budget to 3 s lets it complete (ALLOW).
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        extra_env={"DLP_TEST_SLOW_MS": "400"},
        config_overrides={"service": {"analysis_timeout_seconds": 0.1}},
    )
    assert _decision_text(orch, "anything") == "BLOCK"     # 400 ms > 0.1 s → timeout → BLOCK
    _rewrite_config(orch, {"service": {"analysis_timeout_seconds": 3.0}})
    _poll_until(lambda: _decision_text(orch, "anything"), "ALLOW")


def test_data_pipe_change_requires_restart(make_orchestrator):
    """data_pipe is restart-only: changing it in config.yaml must NOT rebind the
    live server. We change data_pipe AND a hot field (clipboard.failure_mode) in one
    save; once the hot change is observed on the ORIGINAL pipe (proving the reload
    was processed), the original pipe must still serve and the new name must not exist."""
    orch = make_orchestrator(
        policies_fixture="permissive.yaml",
        config_overrides={"analyzer": {"max_extracted_chars": 50}},
    )
    over_cap = "z" * 100
    assert _decision_text(orch, over_cap) == "BLOCK"       # fail_closed default
    bogus = r"\\.\pipe\dlp_test_should_not_rebind"
    _rewrite_config(orch, {"data_pipe": bogus,
                           "clipboard": {"failure_mode": "fail_open"}})
    # Reload processed once the hot field takes effect on the ORIGINAL pipe.
    _poll_until(lambda: _decision_text(orch, over_cap), "ALLOW")
    # The original pipe still serves (just used it) and the new name was never bound.
    with pytest.raises((TimeoutError, OSError)):
        pipe_send(bogus, {"channel": "clipboard", "kind": "text", "text": "hi", "metadata": {}},
                  timeout_seconds=1.0)
    # Best-effort: the agent logged a "requires restart" warning (log location is
    # %PROGRAMDATA%\DLP\logs, redirected by conftest to tmp_dir).
    log = orch.tmp_dir / "DLP" / "logs" / "dlp-agent.log"
    if log.exists():
        assert "data_pipe change requires restart" in log.read_text(encoding="utf-8", errors="replace")
