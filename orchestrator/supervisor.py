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
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from orchestrator.config import OrchestratorConfig

log = logging.getLogger(__name__)

# mitmdump is launched via the current interpreter (sys.executable) running this
# one-liner, NOT via the pip-generated Scripts/mitmdump.exe launcher. Reason:
# pip console-script .exe launchers hardcode the absolute path of the python that
# installed them, so when the embed is relocated (host python-embed -> the VM's
# %ProgramFiles%\DLP\python) the launcher can't find its interpreter and dies
# silently (exit 1, no output). sys.executable is the embed python under the
# service and the .venv python in --foreground — both have mitmproxy importable.
_MITMDUMP_SHIM = "from mitmproxy.tools.main import mitmdump; mitmdump()"


@dataclass
class ChildSpec:
    name: str
    exe: Path
    args: list[str] = field(default_factory=list)
    cwd: Path | None = None
    env_overrides: dict[str, str] = field(default_factory=dict)
    grace_seconds: float = 5.0
    critical_terminate: bool = False
    # Phase E: where this child runs under the LocalSystem service.
    #   "session0"     → spawned once in Session 0 (plain Popen), like Phase C.
    #   "per_session"  → spawned into every interactive session via
    #                     CreateProcessAsUser (the session bridge).
    # Ignored in --foreground mode, where everything runs Session-local as Popen.
    session_scope: str = "session0"
    # Phase E fallback B: this per-session child needs the user's elevated/linked
    # token (e.g. a user-session Controller that must create Global\ kernel objects).
    needs_elevation: bool = False


@dataclass
class _ChildState:
    spec: ChildSpec
    proc: Optional[object] = None          # subprocess.Popen | session.SessionProcess
    spawn_monotonic: float = 0.0
    crash_history: deque = field(default_factory=deque)
    given_up: bool = False
    watcher_thread: Optional[threading.Thread] = None
    pump_thread: Optional[threading.Thread] = None
    file_logger: Optional[logging.Logger] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    session_id: Optional[int] = None       # None → Session-0 child


class Supervisor:
    def __init__(
        self,
        config: OrchestratorConfig,
        repo_root: Path,
        specs: list[ChildSpec],
        log_dir: Path | None = None,
        *,
        service_mode: bool = False,
        session_bridge=None,
    ) -> None:
        self._config = config
        self._repo_root = repo_root
        self._stopping = threading.Event()
        self._states: dict[str, _ChildState] = {}

        # Phase E: in service mode (Session 0) per_session children go through the
        # session bridge; in foreground mode EVERYTHING is a Session-0 Popen child
        # (the Phase C behavior), so session_scope is ignored.
        self._service_mode = service_mode
        self._session_bridge = session_bridge   # lazily resolved to orchestrator.session
        self._session_states: dict[tuple[int, str], _ChildState] = {}
        self._session_sids: dict[int, str] = {}
        self._stopping_sessions: set[int] = set()
        self._session_lock = threading.Lock()

        if log_dir is None:
            log_dir = (
                Path(config.log_dir)
                if config.log_dir
                else Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "DLP" / "logs"
            )
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir = log_dir

        # Per-session proxy redirect targets (used only in service mode).
        self._proxy_server = f"127.0.0.1:{config.proxy_listen_port}"
        self._proxy_bypass = config.proxy_bypass
        inst = (config.raw.get("install") or {})
        state_dir_str = inst.get("state_dir") or ""
        self._state_dir = (
            Path(state_dir_str) if state_dir_str
            else Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "DLP" / "state"
        )

        # Partition specs. In foreground mode every spec is treated as session0.
        self._per_session_specs: list[ChildSpec] = (
            [s for s in specs if s.session_scope == "per_session"] if service_mode else []
        )
        session0_specs = (
            [s for s in specs if s.session_scope == "session0"] if service_mode else list(specs)
        )

        # Validate every exe up front (both scopes) and build one file logger per name.
        self._loggers: dict[str, logging.Logger] = {}
        for spec in specs:
            if not spec.exe.is_file():
                raise FileNotFoundError(
                    f"Supervisor: child {spec.name!r} exe does not exist at {spec.exe}"
                )
            self._loggers[spec.name] = self._build_file_logger(spec.name)

        for spec in session0_specs:
            state = _ChildState(
                spec=spec,
                crash_history=deque(maxlen=config.max_restarts),
            )
            state.file_logger = self._loggers[spec.name]
            self._states[spec.name] = state

    @property
    def _bridge(self):
        """Lazily resolve the session bridge (real ``orchestrator.session`` by default).

        Lazy so that importing supervisor (and foreground mode) never pulls in
        pywin32 session APIs unless service mode actually needs them, and so tests
        can inject a fake bridge via the constructor.
        """
        if self._session_bridge is None:
            from orchestrator import session as _session
            self._session_bridge = _session
        return self._session_bridge

    # ── Public API ───────────────────────────────────────────────────────

    def start_all(self) -> None:
        for state in self._states.values():
            self._spawn(state)
        # Phase E: in service mode, also spawn per-session children into every
        # currently-active interactive session (logon events handle later ones).
        if self._service_mode and self._per_session_specs:
            try:
                sessions = self._bridge.enumerate_interactive_sessions()
            except Exception as exc:  # noqa: BLE001
                log.error("Supervisor: enumerate_interactive_sessions failed: %s", exc)
                sessions = []
            for session_id in sessions:
                self.start_session(session_id)

    def stop_all(self, overall_timeout: float = 15.0) -> None:
        self._stopping.set()
        # Tear down per-session children first (terminate; they can't receive
        # CTRL_BREAK across sessions). This restores each session's proxy too.
        for session_id in list(self._session_sids.keys()):
            self.stop_session(session_id)

        deadline = time.monotonic() + overall_timeout
        if self._service_mode:
            # No console in Session 0 → CTRL_BREAK can't be delivered; terminate
            # directly. mitmdump has no critical teardown.
            for state in self._states.values():
                self._wait_then_kill(state, 0.0)
        else:
            # Foreground: Phase C behavior — BREAK first (overlapping grace), then kill.
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

    # ── Phase E: per-session lifecycle (service mode) ─────────────────────────

    def start_session(self, session_id: int) -> None:
        """Spawn all per-session children into ``session_id`` + set its proxy.

        Idempotent: a child already alive in this session is left running (handles
        duplicate WTS_SESSION_LOGON notifications and start_all/logon overlap).
        """
        if self._stopping.is_set() or not self._per_session_specs:
            return
        bridge = self._bridge
        with self._session_lock:
            self._stopping_sessions.discard(session_id)
            try:
                token = bridge.user_token_for_session(session_id)
            except Exception as exc:  # noqa: BLE001
                # 1008 = ERROR_NO_TOKEN: the session has no interactive user token
                # yet. Expected during the CONNECT-before-LOGON window of a fresh
                # logon — Windows fires WTS_*_CONNECT before WTS_SESSION_LOGON, and
                # the later LOGON event calls start_session again once the token
                # exists. Not an error; log quietly and wait for logon.
                if getattr(exc, "winerror", None) == 1008:
                    log.info("Supervisor: session %d has no interactive user yet "
                             "(connect-before-logon); will start on logon.", session_id)
                else:
                    log.error("Supervisor: no user token for session %d: %s",
                              session_id, exc)
                return
            try:
                sid = bridge.sid_for_token(token)
                self._session_sids[session_id] = sid
                bridge.set_session_proxy(
                    sid, self._proxy_server, self._proxy_bypass, self._state_dir)
            except OSError as exc:
                log.warning("Supervisor: set proxy for session %d failed: %s", session_id, exc)

            for spec in self._per_session_specs:
                key = (session_id, spec.name)
                existing = self._session_states.get(key)
                if existing is not None and self._alive(existing):
                    continue
                state = _ChildState(
                    spec=spec,
                    crash_history=deque(maxlen=self._config.max_restarts),
                    session_id=session_id,
                )
                state.file_logger = self._loggers[spec.name]
                self._session_states[key] = state
                self._spawn_session(state, token)

    def stop_session(self, session_id: int) -> None:
        """Terminate ``session_id``'s children and restore its proxy (idempotent)."""
        with self._session_lock:
            self._stopping_sessions.add(session_id)
            keys = [k for k in self._session_states if k[0] == session_id]
            for k in keys:
                self._terminate_session_child(self._session_states[k])
            for k in keys:
                wt = self._session_states[k].watcher_thread
                if wt is not None:
                    wt.join(timeout=2.0)
                del self._session_states[k]
            sid = self._session_sids.pop(session_id, None)
        if sid:
            try:
                self._bridge.restore_session_proxy(sid, self._state_dir)
            except OSError as exc:
                log.warning("Supervisor: restore proxy for session %d failed: %s",
                            session_id, exc)

    def status_snapshot(self) -> dict[str, dict]:
        snap: dict[str, dict] = {}
        items: list[tuple[str, _ChildState]] = list(self._states.items())
        # Per-session children keyed "<name>@<session_id>".
        items += [(f"{name}@{sid}", st)
                  for (sid, name), st in list(self._session_states.items())]
        for key, st in items:
            with st.lock:
                proc = st.proc
                alive = bool(proc and proc.poll() is None)
                now = time.monotonic()
                in_window = sum(
                    1 for t in st.crash_history if (now - t) <= self._config.restart_window_seconds
                )
                snap[key] = {
                    "pid": proc.pid if alive else None,
                    "alive": alive,
                    "crashes_in_window": in_window,
                    "given_up": st.given_up,
                    "spawn_monotonic": st.spawn_monotonic,
                    "session_id": st.session_id,
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

    # ── Phase E session-child internals ───────────────────────────────────

    @staticmethod
    def _alive(state: _ChildState) -> bool:
        with state.lock:
            proc = state.proc
            return bool(proc and proc.poll() is None)

    def _spawn_session(self, state: _ChildState, token) -> None:
        """CreateProcessAsUser a per-session child (no stdout pump; tracked by handle)."""
        spec = state.spec
        bridge = self._bridge
        use_token = token
        if spec.needs_elevation:
            try:
                use_token = bridge.linked_token(token)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "Supervisor: %s needs an elevated token in session %d but none "
                    "available (%s); giving up on this child.",
                    spec.name, state.session_id, exc)
                state.given_up = True
                return
        try:
            proc = bridge.spawn_as_user(
                use_token,
                str(spec.exe),
                list(spec.args),
                str(spec.cwd) if spec.cwd else None,
                dict(spec.env_overrides) or None,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Supervisor: spawn_as_user failed for %s session %d: %s",
                      spec.name, state.session_id, exc)
            state.given_up = True
            return

        with state.lock:
            state.proc = proc
            state.spawn_monotonic = time.monotonic()
        log.info("Supervisor: spawned %s in session %d pid=%d",
                 spec.name, state.session_id, proc.pid)

        state.watcher_thread = threading.Thread(
            target=self._session_watcher_loop,
            args=(state,),
            daemon=True,
            name=f"sup-watch-{spec.name}-{state.session_id}",
        )
        state.watcher_thread.start()

    def _session_watcher_loop(self, state: _ChildState) -> None:
        spec = state.spec
        proc = state.proc
        if proc is None:
            return
        exit_code = proc.wait()

        if self._stopping.is_set() or state.session_id in self._stopping_sessions:
            log.info("Supervisor: %s (session %d) exited during shutdown (code=%s)",
                     spec.name, state.session_id, exit_code)
            return
        if exit_code == 0:
            log.warning("Supervisor: %s (session %d) exited cleanly; not restarting.",
                        spec.name, state.session_id)
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
                    "Supervisor: %s (session %d) exceeded restart cap (%d in %ds, "
                    "exit=%s); giving up on this child.",
                    spec.name, state.session_id, in_window, window, exit_code)
                state.given_up = True
                return

        log.warning("Supervisor: %s (session %d) crashed (exit=%s); restarting (%d/%d)",
                    spec.name, state.session_id, exit_code, in_window, cap)
        time.sleep(0.5)
        if self._stopping.is_set() or state.session_id in self._stopping_sessions:
            return
        try:
            token = self._bridge.user_token_for_session(state.session_id)
        except Exception as exc:  # noqa: BLE001
            log.error("Supervisor: cannot re-acquire token to restart %s session %d: %s",
                      spec.name, state.session_id, exc)
            return
        self._spawn_session(state, token)

    def _terminate_session_child(self, state: _ChildState) -> None:
        proc = state.proc
        if proc is None or proc.poll() is not None:
            return
        if state.spec.critical_terminate:
            log.info(
                "Supervisor: terminating %s (session %d); Payload.dll hooks "
                "deactivate when the AliveMutex is released on process death.",
                state.spec.name, state.session_id)
        proc.terminate()
        proc.wait(timeout=max(0.5, state.spec.grace_seconds))

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

    addon_script = resolve(config.addon_script)

    # mitmdump's CA confdir: point it at the directory the installer generated
    # the CA into AND added to LocalMachine\Root, so intercepted HTTPS is signed
    # by a trusted CA. Without this, mitmdump mints a fresh untrusted CA in the
    # LocalSystem profile and browser interception silently fails.
    inst = (config.raw.get("install") or {})
    mitm_confdir = inst.get("mitmproxy_confdir") or str(
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "DLP" / "mitmproxy"
    )
    clipboard_exe = resolve(config.clipboard_exe)
    controller_exe = resolve(config.controller_exe)

    # Phase E: Controller placement comes from config (E0 spike result). false →
    # Session 0 (Option A, default); true → per user session (Option B).
    controller_in_user_session = bool(
        (config.raw.get("peripheral_storage") or {}).get("controller_in_user_session", False)
    )

    return [
        ChildSpec(
            name="mitmdump",
            exe=Path(sys.executable),
            args=[
                "-c", _MITMDUMP_SHIM,
                "-s", str(addon_script),
                "--listen-port", str(config.proxy_listen_port),
                "--set", f"confdir={mitm_confdir}",
            ],
            cwd=addon_script.parent,
            # Suppress ANSI escapes so supervisor-mitmdump.log stays readable.
            env_overrides={"NO_COLOR": "1", "TERM": "dumb"},
            grace_seconds=5.0,
            # The proxy binds 127.0.0.1:8080 once and serves every session.
            session_scope="session0",
        ),
        ChildSpec(
            name="clipboard",
            exe=clipboard_exe,
            cwd=clipboard_exe.parent,
            grace_seconds=5.0,
            # Clipboard/window-station is per interactive session → must run there.
            session_scope="per_session",
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
            # Option A (Session 0, cross-session inject) by default; Option B
            # (per session, needs the elevated token for Global\ object creation)
            # when the spike says cross-session injection is unavailable.
            session_scope="per_session" if controller_in_user_session else "session0",
            needs_elevation=controller_in_user_session,
        ),
    ]
