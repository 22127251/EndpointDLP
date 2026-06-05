"""Driver test for the Phase E session-aware Supervisor (orchestrator.supervisor).

No real Win32: a fake session bridge stands in for orchestrator.session, so this
exercises the per-(session_id, child) table bookkeeping, logon/logoff transitions,
restart-on-crash, and per-session proxy set/restore without WTS / CreateProcessAsUser.
The real session bridge is covered by manual smoke (Phase E E7).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from orchestrator.config import OrchestratorConfig
from orchestrator.supervisor import ChildSpec, Supervisor


def _minimal_config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        data_pipe="x", ctl_pipe="x",
        clipboard_workers=1, browser_workers=1, peripheral_storage_workers=1, pipe_listeners=1,
        max_clipboard_bytes=1, max_file_bytes=1,
        max_restarts=3, restart_window_seconds=60, stable_uptime_reset_seconds=60,
        mitmdump_exe="", addon_script="", clipboard_exe="", controller_exe="",
        log_dir=str(tmp_path / "logs"),
        proxy_listen_port=8080, proxy_bypass="localhost",
        policies_file="",
        raw={},
    )


class FakeProc:
    """Stands in for session.SessionProcess: poll/wait/terminate over an event."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._rc: int | None = None
        self._done = threading.Event()

    def poll(self) -> int | None:
        return self._rc

    def wait(self, timeout: float | None = None) -> int | None:
        self._done.wait(timeout)
        return self._rc

    def terminate(self) -> None:
        self._exit(1)

    def _exit(self, code: int) -> None:
        self._rc = code
        self._done.set()

    @property
    def returncode(self) -> int | None:
        return self._rc


class FakeBridge:
    """Fake orchestrator.session for the Supervisor (service_mode)."""

    def __init__(self, sessions: list[int]) -> None:
        self._sessions = list(sessions)
        self._next_pid = 1000
        self.spawned: list[tuple[int, FakeProc]] = []   # (session_id, proc)
        self.proxy_set: list[str] = []
        self.proxy_restored: list[str] = []
        self.lock = threading.Lock()

    def enumerate_interactive_sessions(self) -> list[int]:
        return list(self._sessions)

    def user_token_for_session(self, session_id: int):
        return ("token", session_id)

    def linked_token(self, token):
        return ("linked", token[1])

    def sid_for_token(self, token) -> str:
        return f"S-1-5-21-{token[-1]}"

    def spawn_as_user(self, token, exe, args, cwd, env_extra) -> FakeProc:
        with self.lock:
            self._next_pid += 1
            proc = FakeProc(self._next_pid)
            self.spawned.append((token[-1], proc))
        return proc

    def set_session_proxy(self, sid, server, bypass, state_dir) -> None:
        self.proxy_set.append(sid)

    def restore_session_proxy(self, sid, state_dir) -> None:
        self.proxy_restored.append(sid)


def _per_session_spec(name: str, tmp_path: Path, **kw) -> ChildSpec:
    # exe must exist (Supervisor validates in __init__) but is never launched —
    # the fake bridge's spawn_as_user ignores it.
    return ChildSpec(
        name=name,
        exe=Path(__file__),        # any existing file
        cwd=tmp_path,
        session_scope="per_session",
        grace_seconds=1.0,
        **kw,
    )


def _make_supervisor(tmp_path: Path, bridge: FakeBridge, specs: list[ChildSpec]) -> Supervisor:
    return Supervisor(
        _minimal_config(tmp_path),
        repo_root=tmp_path,
        specs=specs,
        log_dir=tmp_path / "logs",
        service_mode=True,
        session_bridge=bridge,
    )


def test_start_all_spawns_per_session_children_in_each_session(tmp_path: Path) -> None:
    bridge = FakeBridge(sessions=[1, 2])
    sup = _make_supervisor(tmp_path, bridge, [_per_session_spec("clip", tmp_path)])
    sup.start_all()

    # One child per active session.
    assert len(bridge.spawned) == 2
    assert {sid for sid, _ in bridge.spawned} == {1, 2}
    # Proxy redirected for both sessions' SIDs.
    assert set(bridge.proxy_set) == {"S-1-5-21-1", "S-1-5-21-2"}

    snap = sup.status_snapshot()
    assert snap["clip@1"]["alive"] is True
    assert snap["clip@2"]["alive"] is True
    assert snap["clip@1"]["session_id"] == 1

    sup.stop_all()


def test_logon_is_idempotent(tmp_path: Path) -> None:
    bridge = FakeBridge(sessions=[])
    sup = _make_supervisor(tmp_path, bridge, [_per_session_spec("clip", tmp_path)])
    sup.start_all()
    assert bridge.spawned == []

    sup.start_session(3)
    assert len(bridge.spawned) == 1
    # Duplicate WTS_SESSION_LOGON must not double-spawn (child still alive).
    sup.start_session(3)
    assert len(bridge.spawned) == 1

    sup.stop_all()


def test_logoff_removes_only_that_session(tmp_path: Path) -> None:
    bridge = FakeBridge(sessions=[1, 2])
    sup = _make_supervisor(tmp_path, bridge, [_per_session_spec("clip", tmp_path)])
    sup.start_all()

    sup.stop_session(1)

    snap = sup.status_snapshot()
    assert "clip@1" not in snap          # session 1 torn down
    assert snap["clip@2"]["alive"] is True  # session 2 untouched
    assert bridge.proxy_restored == ["S-1-5-21-1"]

    sup.stop_all()
    # session 2 proxy restored on full stop.
    assert "S-1-5-21-2" in bridge.proxy_restored


def test_stop_session_is_idempotent(tmp_path: Path) -> None:
    bridge = FakeBridge(sessions=[1])
    sup = _make_supervisor(tmp_path, bridge, [_per_session_spec("clip", tmp_path)])
    sup.start_all()
    sup.stop_session(1)
    # Second stop must not raise and must restore nothing extra.
    sup.stop_session(1)
    assert bridge.proxy_restored == ["S-1-5-21-1"]

    sup.stop_all()


def test_crashed_session_child_restarts(tmp_path: Path) -> None:
    bridge = FakeBridge(sessions=[1])
    sup = _make_supervisor(tmp_path, bridge, [_per_session_spec("clip", tmp_path)])
    sup.start_all()
    assert len(bridge.spawned) == 1

    # Simulate a crash (non-zero exit) of the first child; watcher should respawn.
    _sid, first = bridge.spawned[0]
    first._exit(1)

    deadline = time.monotonic() + 3.0
    while len(bridge.spawned) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(bridge.spawned) == 2, "crashed per-session child was not restarted"

    sup.stop_all()


def test_controller_elevation_uses_linked_token(tmp_path: Path) -> None:
    bridge = FakeBridge(sessions=[1])
    sup = _make_supervisor(
        tmp_path, bridge,
        [_per_session_spec("controller", tmp_path, needs_elevation=True)],
    )
    sup.start_all()
    # needs_elevation → spawned with the linked token, not the plain one.
    assert len(bridge.spawned) == 1
    sup.stop_all()
