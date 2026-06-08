"""Harness fixtures for Phase A orchestrator validation.

Spawns an isolated orchestrator subprocess per test with a unique pipe name and
private policies/config files under tmp/harness/<uuid>/. The orchestrator runs
in a new process group so we can send CTRL_BREAK_EVENT for clean shutdown.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pytest
import pywintypes
import win32pipe
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FIXTURE_POLICIES_DIR = Path(__file__).parent / "fixture_policies"

# Phase F: unit tests import orchestrator.dispatcher / policy_manager directly,
# which pull in analyzer.engine. engine.py uses a bare `from policy import ...`,
# so analyzer/ must be on sys.path (the orchestrator subprocess does this in
# __main__.py; do the same here for in-process imports). Repo root is added too
# so `import orchestrator` / `import analyzer` resolve regardless of invocation dir.
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "analyzer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_fixture_policy(name: str) -> str:
    return (_FIXTURE_POLICIES_DIR / name).read_text(encoding="utf-8")


@dataclass
class Orchestrator:
    pipe_name: str
    policies_path: Path
    config_path: Path
    log_dir: Path
    tmp_dir: Path
    proc: subprocess.Popen
    admin_pipe: str = ""

    def write_policies(self, yaml_content: str, atomic: bool = True) -> None:
        """Replace policies.yaml. atomic=True uses os.replace (write-temp + rename)."""
        if atomic:
            tmp = self.policies_path.with_suffix(".yaml.tmp")
            tmp.write_text(yaml_content, encoding="utf-8")
            os.replace(tmp, self.policies_path)
        else:
            self.policies_path.write_text(yaml_content, encoding="utf-8")


def _wait_for_pipe_ready(pipe_name: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            win32pipe.WaitNamedPipe(pipe_name, 200)
            return
        except pywintypes.error:
            time.sleep(0.1)
    raise TimeoutError(f"orchestrator pipe {pipe_name} not ready after {timeout_s}s")


def _kill_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        try:
            proc.kill()
        except OSError:
            pass


@pytest.fixture
def make_orchestrator():
    """Factory: spawn an isolated orchestrator subprocess.

    Usage::

        orch = make_orchestrator(
            policies_yaml="policies: []",
            pool_overrides={"browser_workers": 1},
            extra_env={"DLP_TEST_SLOW_MS": "5000"},
        )

    Multiple calls in one test are supported. Teardown stops every spawned proc
    and cleans the tmp dir.
    """
    spawned: list[Orchestrator] = []

    def _make(
        policies_yaml: Optional[str] = None,
        policies_fixture: Optional[str] = None,
        pool_overrides: Optional[dict] = None,
        extra_env: Optional[dict] = None,
        ready_timeout_s: float = 10.0,
    ) -> Orchestrator:
        if policies_yaml is None and policies_fixture is None:
            policies_fixture = "permissive.yaml"
        if policies_fixture is not None:
            policies_yaml = _load_fixture_policy(policies_fixture)

        run_id = uuid.uuid4().hex[:12]
        tmp_dir = _REPO_ROOT / "tmp" / "harness" / run_id
        log_dir = tmp_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        pipe_name = rf"\\.\pipe\dlp_test_{os.getpid()}_{run_id}"
        ctl_pipe_name = rf"\\.\pipe\dlp_test_ctl_{os.getpid()}_{run_id}"
        admin_pipe_name = rf"\\.\pipe\dlp_test_admin_{os.getpid()}_{run_id}"
        policies_path = tmp_dir / "policies.yaml"
        policies_path.write_text(policies_yaml, encoding="utf-8")

        pools = {
            "clipboard_workers": 2,
            "browser_workers": 3,
            "peripheral_storage_workers": 2,
            "pipe_listeners": 4,
        }
        if pool_overrides:
            pools.update(pool_overrides)

        config = {
            "data_pipe": pipe_name,
            "ctl_pipe": ctl_pipe_name,
            "admin_pipe": admin_pipe_name,
            "pools": pools,
            "limits": {"max_clipboard_bytes": 1048576, "max_file_bytes": 104857600},
            "supervisor": {
                "max_restarts": 3,
                "restart_window_seconds": 60,
                "stable_uptime_reset_seconds": 60,
            },
            "paths": {
                "mitmdump_exe": "",
                "addon_script": "interceptors/browser/addon.py",
                "clipboard_exe": "",
                "log_dir": str(log_dir),
            },
            "proxy": {"listen_port": 8080, "bypass": "localhost;127.0.0.1;<local>"},
            "policies_file": str(policies_path),
            # Phase B per-component sections — minimal values; tests don't need
            # the full browser allow/block lists. The orchestrator just relays
            # these over the ctl-pipe; it doesn't consume them itself.
            "clipboard": {"pipe_timeout_ms": 6000},
            "browser": {
                "pipe_timeout_seconds": 5,
                "fail_behavior": "block",
                "temp_dir": "",
                "min_upload_size_bytes": 1024,
                "domain_blocklist": [],
                "upload_url_keywords": ["upload"],
                "extensions": [],
                "mime_types": [],
            },
            "peripheral_storage": {
                "target_processes": ["explorer.exe"],
                "fail_mode": "open",
                "shared_memory_name": f"UsbDlpDriveMap_{run_id}",
                "payload_dll_path": "Payload.dll",
                "transfer_agent": {
                    "connect_timeout_ms": 5000,
                    "analysis_timeout_seconds": 10,
                },
            },
        }
        config_path = tmp_dir / "config.yaml"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

        env = os.environ.copy()
        # Phase C: disable the child-process supervisor for harness orchestrators.
        # The harness tests pipe/dispatch/config-watch behavior, not the supervised
        # children (which are exercised by test_supervisor.py).
        env["DLP_SUPERVISOR_DISABLED"] = "1"
        # Isolate each orchestrator's logs to its own tmp dir. configure_logging
        # derives the log dir from %PROGRAMDATA%; without this every harness
        # orchestrator writes the SHARED %PROGRAMDATA%\DLP\logs\{dlp-agent.log,
        # events.jsonl}, so a single stuck/force-killed process's file handle can
        # cascade into "permission denied" startup crashes across the whole suite.
        # (The supervisor — the only other PROGRAMDATA consumer — is disabled above.)
        env["PROGRAMDATA"] = str(tmp_dir)
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items()})

        proc = subprocess.Popen(
            [sys.executable, "-m", "orchestrator", "--foreground", "--config", str(config_path)],
            cwd=str(_REPO_ROOT),
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            _wait_for_pipe_ready(pipe_name, timeout_s=ready_timeout_s)
        except Exception:
            _kill_proc(proc)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        orch = Orchestrator(
            pipe_name=pipe_name,
            policies_path=policies_path,
            config_path=config_path,
            log_dir=log_dir,
            tmp_dir=tmp_dir,
            proc=proc,
            admin_pipe=admin_pipe_name,
        )
        spawned.append(orch)
        return orch

    yield _make

    for orch in spawned:
        _kill_proc(orch.proc)
        shutil.rmtree(orch.tmp_dir, ignore_errors=True)
