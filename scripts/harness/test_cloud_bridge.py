"""Unit tests for orchestrator.cloud_bridge.CloudBridge.ensure_registered.

These are pure in-process tests (no orchestrator subprocess, no network): we build
a CloudBridge over a minimal fake config and stub the low-level _patch/_post HTTP
helpers, then assert the enrollment decision logic.

Focus: Phase 0 (T7a) hardening — when an agent_id IS configured (the admin-driven
enrollment workflow), a *transient* heartbeat failure must KEEP the configured id
and let the heartbeat loop retry, and must NOT fall back to the admin-only
/register endpoint (which would 403 → standalone until the next service restart).
The genuine 404 ("server doesn't know this id") and empty-id (auto-register) paths
must still work.
"""
from __future__ import annotations

from types import SimpleNamespace

from orchestrator.cloud_bridge import CloudBridge


def _make_bridge(agent_id: str = "") -> CloudBridge:
    """CloudBridge over a minimal fake config (only the attrs __init__ reads)."""
    cfg = SimpleNamespace(
        server_url="http://192.168.6.1:8000",
        server_agent_id=agent_id,
        server_heartbeat_interval=30,
        server_log_sync_interval=300,
        server_enabled=True,
        policies_file="analyzer/policies.yaml",
    )
    return CloudBridge(cfg)


class _Recorder:
    """Records calls and returns a scripted (status, body) per HTTP verb."""

    def __init__(self, patch_result, post_result=None):
        self._patch_result = patch_result
        self._post_result = post_result or (201, {"id": "srv-generated-id"})
        self.patch_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.get_calls: list[str] = []

    def patch(self, path, body=None, timeout=10):
        self.patch_calls.append(path)
        return self._patch_result

    def post(self, path, body, timeout=10):
        self.post_calls.append((path, body))
        return self._post_result

    def get(self, path, timeout=10):
        self.get_calls.append(path)
        return (200, {"items": []})


def _wire(bridge: CloudBridge, rec: _Recorder) -> None:
    bridge._patch = rec.patch
    bridge._post = rec.post
    bridge._get = rec.get


def test_configured_id_transient_failure_keeps_id_no_register():
    """agent_id set + network-down heartbeat (status 0) → keep id, never /register."""
    bridge = _make_bridge(agent_id="cfg-agent-id")
    rec = _Recorder(patch_result=(0, "connection refused"))
    _wire(bridge, rec)

    result = bridge.ensure_registered()

    assert result == "cfg-agent-id"
    assert bridge.agent_id == "cfg-agent-id"
    assert rec.patch_calls == ["/api/v1/agents/cfg-agent-id/heartbeat"]
    # the admin-only /register must NOT have been called
    assert all("/register" not in p for p, _ in rec.post_calls)
    assert rec.post_calls == []


def test_configured_id_server_5xx_keeps_id():
    """agent_id set + server 5xx → still a transient outcome → keep id, no register."""
    bridge = _make_bridge(agent_id="cfg-agent-id")
    rec = _Recorder(patch_result=(503, "service unavailable"))
    _wire(bridge, rec)

    assert bridge.ensure_registered() == "cfg-agent-id"
    assert rec.post_calls == []


def test_configured_id_200_is_verified():
    """agent_id set + heartbeat 200 → verified, returned, no register."""
    bridge = _make_bridge(agent_id="cfg-agent-id")
    rec = _Recorder(patch_result=(200, {"policies": []}))
    _wire(bridge, rec)

    assert bridge.ensure_registered() == "cfg-agent-id"
    assert rec.post_calls == []


def test_configured_id_404_clears_and_registers():
    """agent_id set + genuine 404 → clear id, then auto-register by hostname."""
    bridge = _make_bridge(agent_id="stale-id")
    rec = _Recorder(patch_result=(404, "not found"),
                    post_result=(201, {"id": "new-server-id"}))
    _wire(bridge, rec)

    result = bridge.ensure_registered()

    assert result == "new-server-id"
    assert rec.patch_calls == ["/api/v1/agents/stale-id/heartbeat"]
    # the 404 path must reach /register exactly once
    assert [p for p, _ in rec.post_calls] == ["/api/v1/agents/register"]


def test_empty_id_registers_by_hostname():
    """No agent_id → straight to auto-register (no heartbeat attempt)."""
    bridge = _make_bridge(agent_id="")
    rec = _Recorder(patch_result=(200, {}),  # should never be used
                    post_result=(201, {"id": "fresh-id"}))
    _wire(bridge, rec)

    assert bridge.ensure_registered() == "fresh-id"
    assert rec.patch_calls == []  # no id → no heartbeat probe
    assert [p for p, _ in rec.post_calls] == ["/api/v1/agents/register"]
