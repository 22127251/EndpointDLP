"""Phase F: admin-pipe (dlp-ctl status/reload) end-to-end, plus a deterministic
PolicyManager.force_reload unit test.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import pywintypes
import winerror

from orchestrator.admin_server import AdminServer
from orchestrator.ctl import _send_admin
from orchestrator.policy_manager import PolicyManager

_FIXTURE_POLICIES = Path(__file__).parent / "fixture_policies" / "permissive.yaml"


def _admin(orch, request, timeout_s: float = 8.0) -> dict:
    """Send an admin request, retrying until the admin-pipe is up.

    The admin-pipe DACL is SYSTEM + Administrators only, so a non-elevated test
    run is (correctly) denied — in that case we skip rather than fail.
    """
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            return _send_admin(orch.admin_pipe, request)
        except pywintypes.error as exc:
            if exc.winerror == winerror.ERROR_ACCESS_DENIED:
                pytest.skip("admin-pipe requires elevation (Administrators-only DACL)")
            last = exc  # pipe not created yet — retry
            time.sleep(0.2)
    raise AssertionError(f"admin-pipe never answered: {last}")


def test_handle_request_dispatch():
    """Pure unit test of the command dispatch (no pipe / no DACL)."""
    status = {"uptime_seconds": 1.0, "inflight": {"clipboard": 0},
              "children": {}, "service_mode": False,
              "last_config_reload": "t", "last_policy_reload": "t"}
    srv = AdminServer(
        config=None,
        status_provider=lambda: status,
        reload_callback=lambda: {"reloaded": ["policies"]},
    )
    s = srv.handle_request({"cmd": "status"})
    assert s["ok"] is True and s["uptime_seconds"] == 1.0
    r = srv.handle_request({"cmd": "reload"})
    assert r["ok"] is True and r["reloaded"] == ["policies"]
    u = srv.handle_request({"cmd": "bogus"})
    assert u["ok"] is False and "unknown cmd" in u["error"]


def test_admin_status(make_orchestrator):
    orch = make_orchestrator(policies_fixture="permissive.yaml")
    resp = _admin(orch, {"cmd": "status"})
    assert resp["ok"] is True
    assert isinstance(resp["uptime_seconds"], (int, float))
    assert set(resp["inflight"]) == {"clipboard", "browser", "peripheral_storage"}
    # Harness runs with DLP_SUPERVISOR_DISABLED, so no supervised children.
    assert resp["children"] == {}
    assert "last_config_reload" in resp and "last_policy_reload" in resp


def test_admin_reload_applies_both(make_orchestrator):
    orch = make_orchestrator(policies_fixture="permissive.yaml")
    resp = _admin(orch, {"cmd": "reload"})
    assert resp["ok"] is True
    # Force-reload (Option A) unconditionally re-applies both valid files.
    assert set(resp["reloaded"]) == {"config", "policies"}
    assert "errors" not in resp


def test_admin_unknown_cmd(make_orchestrator):
    orch = make_orchestrator(policies_fixture="permissive.yaml")
    resp = _admin(orch, {"cmd": "bogus"})
    assert resp["ok"] is False
    assert "unknown cmd" in resp["error"]


def test_policy_force_reload_unconditional(tmp_path):
    policies = tmp_path / "policies.yaml"
    policies.write_text(_FIXTURE_POLICIES.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = SimpleNamespace(policies_file=str(policies))
    pm = PolicyManager(cfg)
    # Stop the watchdog so it can't race the manual force_reload assertions.
    pm._observer.stop()
    pm._observer.join()
    try:
        first_ts = pm.last_reload_time()
        time.sleep(0.05)
        # Force-reload rebuilds even when the file is unchanged (Option A).
        assert pm.force_reload() is True
        assert pm.last_reload_time() > first_ts
        # A broken policies file → rebuild fails, old engine kept, returns False.
        policies.write_text("}{ not valid yaml :::", encoding="utf-8")
        assert pm.force_reload() is False
    finally:
        pm.stop()
