"""Child process spawn, health watch, and restart loop.

Phase C foreground-mode supervisor. Spawns mitmdump, ClipboardInterceptor.exe,
and UsbDlpController.exe alongside the orchestrator and restarts them on crash
up to a configurable cap. Shutdown is via CTRL_BREAK_EVENT directed at each
child's own process group — this fires the child's existing Console.CancelKeyPress
handler so Controller can release the AliveMutex (deactivating Payload.dll hooks
in explorer.exe) before exit.

Design notes locked by the Phase C plan:
- One watcher thread + one stdout-pump thread per child.
- CREATE_NEW_PROCESS_GROUP is mandatory so CTRL_BREAK_EVENT can be directed
  at a single child (CTRL_C_EVENT can't be — it always hits the whole console
  group, including the orchestrator).
- stdin=DEVNULL so children can't pull keystrokes from the orchestrator's console.
- Per-child log file under <log_dir>/supervisor-<name>.log with propagate=False
  so child output stays out of dlp-agent.log.
- Past the restart cap: give up on that child only; other children stay supervised.
- Controller has critical_terminate=True: a forced TerminateProcess of Controller
  is logged at CRITICAL because it skips the alive-mutex release and leaves
  Payload.dll hooks live in explorer.exe.

TODO(Phase E): wrap each child in a Win32 Job Object with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so an orchestrator hard-kill (Task Manager
"End Task") doesn't orphan children. Phase C accepts orphan risk for foreground/dev.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from orchestrator.config import OrchestratorConfig

log = logging.getLogger(__name__)


@dataclass
class ChildSpec:
    name: str
    exe: Path
    args: list[str] = field(default_factory=list)
    cwd: Path | None = None
    env_overrides: dict[str, str] = field(default_factory=dict)
    grace_seconds: float = 5.0
    critical_terminate: bool = False


@dataclass
class _ChildState:
    spec: ChildSpec
    proc: Optional[subprocess.Popen] = None
    spawn_monotonic: float = 0.0
    crash_history: deque = field(default_factory=deque)
    given_up: bool = False
    watcher_thread: Optional[threading.Thread] = None
    pump_thread: Optional[threading.Thread] = None
    file_logger: Optional[logging.Logger] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class Supervisor:
    def __init__(
        self,
        config: OrchestratorConfig,
        repo_root: Path,
        specs: list[ChildSpec],
        log_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._repo_root = repo_root
        self._stopping = threading.Event()
        self._states: dict[str, _ChildState] = {}

        if log_dir is None:
            log_dir = (
                Path(config.log_dir)
                if config.log_dir
                else Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "DLP" / "logs"
            )
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir = log_dir

        for spec in specs:
            if not spec.exe.is_file():
                raise FileNotFoundError(
                    f"Supervisor: child {spec.name!r} exe does not exist at {spec.exe}"
                )
            state = _ChildState(
                spec=spec,
                crash_history=deque(maxlen=config.max_restarts),
            )
            state.file_logger = self._build_file_logger(spec.name)
            self._states[spec.name] = state

    # ── Public API ───────────────────────────────────────────────────────

    def start_all(self) -> None:
        for state in self._states.values():
            self._spawn(state)

    def stop_all(self, overall_timeout: float = 15.0) -> None:
        self._stopping.set()
        deadline = time.monotonic() + overall_timeout
        # Send BREAK to all children first so their grace windows overlap.
        for state in self._states.values():
            self._send_break(state)
        for state in self._states.values():
            remaining = max(0.1, deadline - time.monotonic())
            self._wait_then_kill(state, min(remaining, state.spec.grace_seconds))
        for state in self._states.values():
            if state.watcher_thread is not None:
                state.watcher_thread.join(timeout=2.0)
            if state.pump_thread is not None:
                state.pump_thread.join(timeout=2.0)

    def status_snapshot(self) -> dict[str, dict]:
        snap: dict[str, dict] = {}
        for name, st in self._states.items():
            with st.lock:
                proc = st.proc
                alive = bool(proc and proc.poll() is None)
                now = time.monotonic()
                in_window = sum(
                    1 for t in st.crash_history if (now - t) <= self._config.restart_window_seconds
                )
                snap[name] = {
                    "pid": proc.pid if alive else None,
                    "alive": alive,
                    "crashes_in_window": in_window,
                    "given_up": st.given_up,
                    "spawn_monotonic": st.spawn_monotonic,
                }
        return snap

    # ── Internals ────────────────────────────────────────────────────────

    def _spawn(self, state: _ChildState) -> None:
        spec = state.spec
        env = os.environ.copy()
        env.update(spec.env_overrides)
        try:
            proc = subprocess.Popen(
                [str(spec.exe), *spec.args],
                cwd=str(spec.cwd) if spec.cwd else None,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            log.error("Supervisor: failed to spawn %s: %s", spec.name, exc)
            state.given_up = True
            return

        with state.lock:
            state.proc = proc
            state.spawn_monotonic = time.monotonic()

        log.info("Supervisor: spawned %s pid=%d cwd=%s", spec.name, proc.pid, spec.cwd)

        state.pump_thread = threading.Thread(
            target=self._pump_loop,
            args=(state, proc.stdout),
            daemon=True,
            name=f"sup-pump-{spec.name}",
        )
        state.pump_thread.start()

        state.watcher_thread = threading.Thread(
            target=self._watcher_loop,
            args=(state,),
            daemon=True,
            name=f"sup-watch-{spec.name}",
        )
        state.watcher_thread.start()

    def _watcher_loop(self, state: _ChildState) -> None:
        spec = state.spec
        proc = state.proc
        if proc is None:
            return
        exit_code = proc.wait()

        if self._stopping.is_set():
            log.info("Supervisor: %s exited during shutdown (code=%d)", spec.name, exit_code)
            return

        if exit_code == 0:
            log.warning("Supervisor: %s exited cleanly (code=0); not restarting.", spec.name)
            return

        now = time.monotonic()
        with state.lock:
            uptime = now - state.spawn_monotonic
            if uptime >= self._config.stable_uptime_reset_seconds:
                state.crash_history.clear()
            state.crash_history.append(now)
            window = self._config.restart_window_seconds
            cap = self._config.max_restarts
            in_window = sum(1 for t in state.crash_history if (now - t) <= window)

            if in_window >= cap:
                log.error(
                    "Supervisor: %s exceeded restart cap (%d crashes in %ds, exit=%d); "
                    "giving up on this child. Other children remain supervised.",
                    spec.name, in_window, window, exit_code,
                )
                state.given_up = True
                return

        log.warning(
            "Supervisor: %s crashed (exit=%d); restarting (%d/%d in last %ds)",
            spec.name, exit_code, in_window, cap, window,
        )
        time.sleep(0.5)
        if self._stopping.is_set():
            return
        self._spawn(state)
        # The new watcher thread (spawned by _spawn) takes over; this one exits.

    def _pump_loop(self, state: _ChildState, stdout) -> None:
        name = state.spec.name
        flogger = state.file_logger
        try:
            for raw in stdout:
                line = raw.rstrip("\r\n")
                if flogger is not None:
                    flogger.info(line)
        except Exception as exc:  # noqa: BLE001
            log.warning("Supervisor: pump thread for %s ended: %s", name, exc)
        finally:
            try:
                stdout.close()
            except Exception:  # noqa: BLE001
                pass

    def _send_break(self, state: _ChildState) -> None:
        proc = state.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            log.info(
                "Supervisor: sent CTRL_BREAK_EVENT to %s pid=%d",
                state.spec.name, proc.pid,
            )
        except (OSError, ValueError) as exc:
            log.warning("Supervisor: send_signal to %s failed: %s", state.spec.name, exc)

    def _wait_then_kill(self, state: _ChildState, grace: float) -> None:
        proc = state.proc
        if proc is None:
            return
        try:
            proc.wait(timeout=grace)
            log.info(
                "Supervisor: %s exited gracefully (code=%s)",
                state.spec.name, proc.returncode,
            )
            return
        except subprocess.TimeoutExpired:
            pass

        if state.spec.critical_terminate:
            log.critical(
                "Supervisor: %s did not exit within %.1fs — calling TerminateProcess. "
                "This SKIPS the alive-mutex release; injected Payload.dll hooks in "
                "explorer.exe may remain live until explorer is restarted.",
                state.spec.name, grace,
            )
        else:
            log.warning(
                "Supervisor: %s did not exit within %.1fs — calling TerminateProcess.",
                state.spec.name, grace,
            )
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.error("Supervisor: terminate of %s failed: %s", state.spec.name, exc)

    def _build_file_logger(self, name: str) -> logging.Logger:
        logger = logging.getLogger(f"orchestrator.supervisor.{name}")
        logger.setLevel(logging.INFO)
        # Keep child lines out of dlp-agent.log (the root logger's file handler).
        logger.propagate = False
        for h in list(logger.handlers):
            logger.removeHandler(h)
        handler = logging.handlers.RotatingFileHandler(
            self._log_dir / f"supervisor-{name}.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        return logger


def build_default_specs(config: OrchestratorConfig, repo_root: Path) -> list[ChildSpec]:
    """Construct the three Phase C ChildSpecs from config + repo root.

    Path-resolution rules:
    - Absolute paths in `paths.*` are used as-is.
    - Relative paths are resolved against repo_root.
    - `paths.mitmdump_exe` empty → fall back to <repo_root>/.venv/Scripts/mitmdump.exe.
    """
    def resolve(rel_or_abs: str) -> Path:
        p = Path(rel_or_abs)
        return p if p.is_absolute() else (repo_root / p).resolve()

    mitmdump_str = config.mitmdump_exe.strip() or str(
        repo_root / ".venv" / "Scripts" / "mitmdump.exe"
    )
    mitmdump_exe = resolve(mitmdump_str)
    addon_script = resolve(config.addon_script)
    clipboard_exe = resolve(config.clipboard_exe)
    controller_exe = resolve(config.controller_exe)

    return [
        ChildSpec(
            name="mitmdump",
            exe=mitmdump_exe,
            args=[
                "-s", str(addon_script),
                "--listen-port", str(config.proxy_listen_port),
            ],
            cwd=addon_script.parent,
            # Suppress ANSI escapes so supervisor-mitmdump.log stays readable.
            env_overrides={"NO_COLOR": "1", "TERM": "dumb"},
            grace_seconds=5.0,
        ),
        ChildSpec(
            name="clipboard",
            exe=clipboard_exe,
            cwd=clipboard_exe.parent,
            grace_seconds=5.0,
        ),
        ChildSpec(
            name="controller",
            exe=controller_exe,
            cwd=controller_exe.parent,
            # ManagementEventWatcher.Stop() can stall 15-30s during dispose;
            # Controller releases the alive mutex BEFORE that, so 10s covers
            # the part that matters (hook deactivation) plus normal teardown.
            grace_seconds=10.0,
            # Forced kill skips the mutex release → Payload.dll hooks remain live.
            critical_terminate=True,
        ),
    ]
