"""Lifecycle smoke test for orchestrator.supervisor.Supervisor.

Phase C IT-C5. Uses python.exe as a stub child to exercise the spawn/state-track/
forced-kill/thread-join path without needing the C# binaries on disk.

Note: a vanilla `python.exe -c "..."` has no SIGBREAK handler, so this test
deliberately exercises the terminate-fallback path (grace expires → terminate()).
The graceful-BREAK path with .NET children is verified manually per the plan's
end-to-end smoke section.
"""

from __future__ import annotations

import sys
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
        proxy_listen_port=8080, proxy_bypass="",
        policies_file="",
        raw={},
    )


def test_supervisor_lifecycle(tmp_path: Path) -> None:
    spec = ChildSpec(
        name="stub",
        exe=Path(sys.executable),
        args=["-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        grace_seconds=1.5,
        critical_terminate=False,
    )
    sup = Supervisor(
        _minimal_config(tmp_path),
        repo_root=tmp_path,
        specs=[spec],
        log_dir=tmp_path / "logs",
    )
    sup.start_all()
    # Give the spawn thread time to register the pid.
    time.sleep(0.5)
    snap = sup.status_snapshot()
    assert snap["stub"]["alive"] is True
    assert snap["stub"]["pid"] is not None

    sup.stop_all()

    after = sup.status_snapshot()
    assert after["stub"]["alive"] is False
    assert after["stub"]["pid"] is None

    assert (tmp_path / "logs" / "supervisor-stub.log").exists()


def test_supervisor_missing_exe_raises(tmp_path: Path) -> None:
    spec = ChildSpec(
        name="ghost",
        exe=tmp_path / "does_not_exist.exe",
        cwd=tmp_path,
    )
    try:
        Supervisor(
            _minimal_config(tmp_path),
            repo_root=tmp_path,
            specs=[spec],
            log_dir=tmp_path / "logs",
        )
    except FileNotFoundError as exc:
        assert "ghost" in str(exc)
        assert "does_not_exist.exe" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError for missing child exe")


def test_supervisor_stop_all_is_idempotent(tmp_path: Path) -> None:
    spec = ChildSpec(
        name="stub",
        exe=Path(sys.executable),
        args=["-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        grace_seconds=1.0,
    )
    sup = Supervisor(
        _minimal_config(tmp_path),
        repo_root=tmp_path,
        specs=[spec],
        log_dir=tmp_path / "logs",
    )
    sup.start_all()
    time.sleep(0.3)
    sup.stop_all()
    # Second call must not raise and must return quickly.
    t0 = time.monotonic()
    sup.stop_all()
    assert (time.monotonic() - t0) < 2.0
