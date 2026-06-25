# Phase C — Foreground Supervisor: spawn, watch, restart mitmdump / ClipboardInterceptor / Controller

> Cross-reference key:
> - **IT-C1 … IT-C5** are the five implementation tasks in *Implementation tasks*.
> - **Q1 … Q3** are user-confirmed Phase C decisions (shutdown signal, child logging, restart-cap action).
> - **R1 … R7** are tracked risks in *Risks*.
> - **child** = one of `mitmdump`, `clipboard` (ClipboardInterceptor.exe), `controller` (UsbDlpController.exe). Always lowercase in code, configs, and logger names.

## Context

End of Phase B: every long-running component (orchestrator, ClipboardInterceptor, browser addon, Controller) reads its initial config from `config.yaml` and gets push updates via the ctl-pipe. But **only the orchestrator is started by `python -m orchestrator --foreground`** — the other three are launched by hand in separate shells. That makes the system unusable as a single product: an operator has to remember the right launch command, the right CWD, the right config-discovery env var, and has to manually restart any child that crashes.

Phase C closes that gap **for foreground/dev mode only** (LocalSystem service install lands in Phase E, installer in Phase D). After Phase C:

1. `python -m orchestrator --foreground` spawns mitmdump, ClipboardInterceptor.exe, and Controller.exe alongside the orchestrator. The operator runs **one** command and gets the whole agent.
2. Each child has a restart watcher: crashes within `restart_window_seconds` (default 60s) trigger automatic respawn, up to `max_restarts` (default 3). After the cap, **the offending child is given up on; the other two stay supervised** (Q3). After `stable_uptime_reset_seconds` (default 60s) of stable running, the crash counter resets.
3. Ctrl+C on the orchestrator shuts every child down **cleanly**: each child gets a Windows console `CTRL_BREAK_EVENT` directed at its own process group (Q1). This invokes the child's existing `Console.CancelKeyPress` handler (or mitmproxy's SIGBREAK path), so the child runs its normal teardown — critically, Controller releases the `AliveMutex` it owns, which is the signal that makes the injected Payload.dll hooks deactivate inside `explorer.exe`. If a child does not exit within its grace window, the supervisor falls back to `proc.terminate()` (Windows `TerminateProcess`) — **for Controller this is logged at CRITICAL** because forced kill leaves hooks live in `explorer.exe` until explorer is restarted.
4. Each child's stdout+stderr is captured into its own rotating file under `%PROGRAMDATA%\DLP\logs\supervisor-<child>.log` (Q2). The orchestrator's own log file (`dlp-agent.log`) is untouched.

**Out of scope for Phase C** — listed here so they aren't re-litigated during implementation:
- TransferAgent supervision — never. TransferAgent is per-file, launched by ShellExtension, and must not be hooked (project memory).
- Windows Service mode, `CreateProcessAsUser`, session-aware spawning, named-pipe ACL hardening — all Phase E.
- Job-Object-based orphan prevention — Phase E (see R3).
- Exposing supervisor status via the ctl-pipe so `dlp-ctl status` can show child health — Phase F. Phase C ships an in-process `Supervisor.status_snapshot()` method that Phase F will reuse; nothing else reads it yet.

## Locked decisions (this session)

| # | Decision | Source |
|---|----------|--------|
| Q1 | **Shutdown signal: `CTRL_BREAK_EVENT` directed at each child's own process group.** Spawn each child with `subprocess.CREATE_NEW_PROCESS_GROUP`, then call `proc.send_signal(signal.CTRL_BREAK_EVENT)` at stop. .NET's `Console.CancelKeyPress` **always** fires on `CTRL_BREAK_EVENT` (Microsoft docs), so Controller's and ClipboardInterceptor's existing handlers are triggered without any child-side code change. `CTRL_C_EVENT` was rejected because it cannot be directed at a single process — it hits the whole console group, including the orchestrator itself. Named-event wait was rejected as more code with no functional advantage. | User Q1 |
| Q2 | **Per-child rotating log files** under `%PROGRAMDATA%\DLP\logs\supervisor-<child>.log` (5 MB × 3 backups). Each file uses a dedicated logger `orchestrator.supervisor.<child>` with `propagate=False` so child lines do NOT also flow into `dlp-agent.log`. Format string carries only `%(asctime)s %(message)s` because the child's own line already starts with its own prefix (`[Controller]`, `[DLP]`, etc.). | User Q2 |
| Q3 | **Past the restart cap: give up on that child; other children stay supervised.** Log a loud `ERROR` line with crash count and window. Do NOT panic-stop the orchestrator. | User Q3 |
| D1 | **One watcher thread + one stdout-pump thread per child** (six helper threads total for three children) — NOT a single `WaitForMultipleObjects`. Justification: only three children, no kernel multiplexing needed; per-child isolation (separate `deque`, separate logger, separate lock) is the simpler design. Each watcher blocks on `proc.wait()`; each pump iterates `for line in proc.stdout`. | Plan agent |
| D2 | **`CREATE_NEW_PROCESS_GROUP` is mandatory** — without it `proc.send_signal(CTRL_BREAK_EVENT)` raises `ValueError` on Windows. This flag also means the child shares the orchestrator's console window (no new window pops up) but is exempt from the orchestrator's own Ctrl+C, which is exactly what we want — only the supervisor sends BREAK. | Python 3.13 docs + plan agent |
| D3 | **`stdin=subprocess.DEVNULL` for all three children.** Without it, the child inherits the orchestrator's console stdin handle and may accidentally consume keystrokes intended for the orchestrator. | Plan agent |
| D4 | **`build_default_specs(config, repo_root)` helper** lives in `orchestrator/supervisor.py`. It applies the path-resolution rules (relative paths resolved against repo root, `mitmdump_exe` empty → `.venv\Scripts\mitmdump.exe` fallback) and constructs the three `ChildSpec`s with correct CWDs (`interceptors/browser/` for mitmdump because addon does bare `import pipe_client`; the exe's own dir for clipboard and controller). Keeps `__main__.py` thin. | Plan agent |
| D5 | **mitmdump env overrides** to suppress ANSI codes in the captured log file: `NO_COLOR=1`, `TERM=dumb`. Otherwise `supervisor-mitmdump.log` accumulates `\x1b[...]` escape sequences from mitmproxy's TTY banner. | Plan agent |
| D6 | **Path discovery rule.** `paths.controller_exe` / `paths.clipboard_exe` / `paths.mitmdump_exe` may be absolute or relative; relative paths are resolved against the repo root (= `Path(orchestrator/__main__.py).parent.parent`). If the resolved file does not exist on disk, `Supervisor.__init__` raises `FileNotFoundError` with the absolute resolved path in the message — fail loudly at startup, not deep inside `subprocess.Popen`. | Plan agent |
| D7 | **`Supervisor.stop_all()` is idempotent.** Called once from `except KeyboardInterrupt` (FIRST, before `server.stop()`, so Controller releases the alive mutex while the orchestrator's pipes are still up); called again from `finally` (guarded by `if "supervisor" in locals()`). The second call sees `_stopping` already set, finds the children already dead, and returns quickly. | Plan agent |
| D8 | **Exit code 0 = "child decided to exit cleanly"; no restart and no crash count.** Non-zero exit during normal operation = crash. Exit during `_stopping` set = expected shutdown, no count regardless of code. | Plan agent |
| D9 | **Per-child grace windows.** mitmdump and clipboard: 5 s. controller: 10 s, because `ManagementEventWatcher.Stop()` in `ProcessMonitor.Dispose()` can block 15–30 s; controller's `Program.cs:190` releases the mutex BEFORE the using-block disposal runs, so the *hook-deactivation* part happens fast even if the process-exit part stalls. 10 s is enough for the part we care about (mutex release) plus normal teardown. | Plan agent + Controller/Program.cs:185-198 |
| D10 | **`critical_terminate` flag** on `ChildSpec` — only set for controller. When grace expires and the supervisor falls back to `proc.terminate()`, controller logs at `CRITICAL` (other two log at `WARNING`), because forced kill of controller skips the mutex release and leaves Payload.dll hooks live in `explorer.exe`. | Plan agent |

## Critical files

**Edits**
- `D:\Code\GithubPublishEndpointDLP\orchestrator\supervisor.py` — replace the 1-line stub (currently a docstring only) with the full implementation (~250 LOC). [IT-C1]
- `D:\Code\GithubPublishEndpointDLP\orchestrator\__main__.py` — add Supervisor import, instantiation and `start_all()` after `t.start()` (currently line 122), `stop_all()` calls in the `except KeyboardInterrupt` block (FIRST, before `server.stop()`) and in `finally` (guarded). [IT-C4]
- `D:\Code\GithubPublishEndpointDLP\orchestrator\config.py` — add `controller_exe: str` field to `OrchestratorConfig` (after the existing `clipboard_exe` field at line 24), and add `controller_exe=paths.get(...)` to the `load_config()` return (after line 62). [IT-C2]
- `D:\Code\GithubPublishEndpointDLP\config.yaml` — add `controller_exe:` line in the `paths:` block (currently lines 24-28). [IT-C2]

**New**
- (optional) `D:\Code\GithubPublishEndpointDLP\scripts\harness\test_supervisor.py` — one lifecycle smoke test using `python.exe` as a stub child. [IT-C5]

**Reused (no edits, important for context)**
- `orchestrator/__main__.py:66-137` — `_run_foreground` is the only call site of the Supervisor. Pipe-server thread (line 121-122) MUST start before `Supervisor.start_all()` so children can connect to both `data_pipe` and `ctl_pipe` immediately.
- `interceptors/peripheral_storage/Controller/Program.cs:37-42` — existing `Console.CancelKeyPress` handler. `CTRL_BREAK_EVENT` triggers this verbatim; no source change needed.
- `interceptors/peripheral_storage/Controller/Program.cs:185-198` — proves that the mutex-release-before-disposal pattern is already in place. Verification step 4 asserts the `[Controller] Releasing mutex — hooks deactivating...` line appears in `supervisor-controller.log` within 1 s of Ctrl+C.
- `src/ClipboardInterceptor/Program.cs:72-89` — existing `Console.CancelKeyPress` handler. Same story as Controller.
- `interceptors/browser/addon.py:181-186` — existing `done()` hook stops the CtlPipeSubscriber thread. Note: mitmproxy's `done()` reportedly doesn't always fire on Windows SIGINT (web research); benign because the subscriber thread is `daemon=True`. [R2]
- `orchestrator/ctl_server.py:100-121` — stop pattern reference (set `_stop` event + throwaway connect to unblock accept). Supervisor's `stop_all` follows a similar shape but uses signal delivery instead of pipe-unblock.
- `orchestrator/config_watcher.py:81-85` — stop pattern reference (`observer.stop() + observer.join(timeout=2.0)`).
- `orchestrator/logging_setup.py:7-24` — `configure_logging` stays unchanged. Supervisor wires its own `RotatingFileHandler` per child via `_build_file_logger` in `__init__`, with `propagate=False` to avoid double-write.

## Implementation tasks

Ordered so that every commit point compiles and runs.

### IT-C1. Implement `orchestrator/supervisor.py`

**Goal:** the Supervisor class, ChildSpec dataclass, and `build_default_specs` helper exist; nothing wires them yet.

Top-level shape (full code is filled in during implementation; this plan locks the API surface):

```python
@dataclass
class ChildSpec:
    name: str                     # "mitmdump" | "clipboard" | "controller"
    exe: Path                     # absolute, validated in Supervisor.__init__
    args: list[str] = field(default_factory=list)
    cwd: Path | None = None       # absolute
    env_overrides: dict[str, str] = field(default_factory=dict)
    grace_seconds: float = 5.0    # CTRL_BREAK→terminate fallback window (D9)
    critical_terminate: bool = False  # log CRITICAL on forced kill (D10) — controller only

class Supervisor:
    def __init__(self, config: OrchestratorConfig, repo_root: Path,
                 specs: list[ChildSpec], log_dir: Path | None = None) -> None: ...
    def start_all(self) -> None: ...
    def stop_all(self, overall_timeout: float = 15.0) -> None: ...
    def status_snapshot(self) -> dict[str, dict]: ...

def build_default_specs(config: OrchestratorConfig, repo_root: Path) -> list[ChildSpec]: ...
```

Implementation rules (lock these in code, not just docstrings):

- **Spawn (`_spawn`)**: `subprocess.Popen([str(spec.exe), *spec.args], cwd=str(spec.cwd) if spec.cwd else None, env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, bufsize=1, text=True, encoding="utf-8", errors="replace")`. Then start two daemon threads named `sup-watch-<name>` and `sup-pump-<name>`.
- **Watcher (`_watcher_loop`)**: blocks on `proc.wait()`. On return:
  - If `self._stopping.is_set()`: log and exit. **No restart, no crash count regardless of exit code** (D8 second clause).
  - Elif `exit_code == 0`: log WARNING "exited cleanly; not restarting" and exit (D8).
  - Else (crash): apply restart bookkeeping (see below), then either `_spawn(state)` and return (the new watcher thread takes over), or set `state.given_up = True` and exit. After the `_spawn` recursion-replacement, a single 0.5 s `time.sleep(0.5)` backoff prevents hot-loop on a fast-crashing child.
- **Restart bookkeeping**: per-child `crash_history = deque(maxlen=max_restarts)` of `time.monotonic()` timestamps. On each crash:
  1. If `(now - state.spawn_monotonic) >= stable_uptime_reset_seconds`, `state.crash_history.clear()` BEFORE appending — stable-running resets the counter.
  2. Append `now`.
  3. `in_window = sum(1 for t in state.crash_history if (now - t) <= restart_window_seconds)`.
  4. If `in_window >= max_restarts`: log ERROR `"<name> exceeded restart cap (<N> crashes in <W>s, exit=<code>); giving up on this child. Other children remain supervised."`, set `given_up = True`, return.
  5. Else: log WARNING, sleep 0.5 s, re-check `_stopping`, then `_spawn`.

  **Why iterate instead of `len(deque)`**: even though `maxlen=max_restarts`, the deque could hold entries older than the window if `stable_uptime_reset_seconds` hadn't fired. Counting *in-window* entries is the correct condition.

- **Pump (`_pump_loop`)**: `for raw in stdout: state.file_logger.info(raw.rstrip("\r\n"))`. Exits naturally when the child closes stdout (process exit). Wrap in `try/except` so a pump-thread crash doesn't take down the orchestrator.
- **`_build_file_logger`**: called once per child in `Supervisor.__init__`. Logger name `orchestrator.supervisor.<name>`. `propagate=False`. Single `RotatingFileHandler(log_dir / f"supervisor-{name}.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")`. Formatter `"%(asctime)s %(message)s"`. Defensive: clear pre-existing handlers on the logger (safe if Supervisor is rebuilt in tests).
- **`_send_break`**: short-circuit if `proc.poll() is not None`; call `proc.send_signal(signal.CTRL_BREAK_EVENT)`; catch `(OSError, ValueError)` and log WARNING.
- **`_wait_then_kill(state, grace)`**: `proc.wait(timeout=grace)`. On `TimeoutExpired`: log (CRITICAL if `critical_terminate`, else WARNING — D10), then `proc.terminate()` + `proc.wait(timeout=2.0)`.
- **`stop_all`**: set `_stopping`; **send `CTRL_BREAK_EVENT` to all three children first** (parallel signal), then `_wait_then_kill` each within an overall budget of 15 s. Finally join helper threads with `timeout=2.0`. Idempotent (D7) — every helper short-circuits on `proc.poll()` and the second call is a no-op.
- **`status_snapshot`**: per-child lock-guarded copy of `{pid, alive, crashes_in_window, given_up, spawn_monotonic}`. Used by future Phase F `dlp-ctl status`; Phase C only exercises it from the smoke test.

`build_default_specs(config, repo_root)`:

```python
def build_default_specs(config: OrchestratorConfig, repo_root: Path) -> list[ChildSpec]:
    def resolve(rel_or_abs: str) -> Path:
        p = Path(rel_or_abs)
        return p if p.is_absolute() else (repo_root / p).resolve()

    mitmdump_str = config.mitmdump_exe.strip() or str(repo_root / ".venv" / "Scripts" / "mitmdump.exe")
    mitmdump_exe = resolve(mitmdump_str)
    addon_script = resolve(config.addon_script)
    clipboard_exe = resolve(config.clipboard_exe)
    controller_exe = resolve(config.controller_exe)

    return [
        ChildSpec(
            name="mitmdump",
            exe=mitmdump_exe,
            args=["-s", str(addon_script),
                  "--listen-port", str(config.proxy_listen_port)],
            cwd=addon_script.parent,                # interceptors/browser/ — D4
            env_overrides={"NO_COLOR": "1", "TERM": "dumb"},   # D5
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
            grace_seconds=10.0,                     # D9
            critical_terminate=True,                # D10
        ),
    ]
```

**Validates:** `python -c "from orchestrator.supervisor import Supervisor, ChildSpec, build_default_specs; print('ok')"` from repo root (in `.venv`). No `--foreground` wiring yet — that's IT-C4.

### IT-C2. Add `controller_exe` to config

**Goal:** Supervisor can find Controller.exe via the same `paths:` mechanism as the other two children.

- `config.yaml` — inside the `paths:` block (currently lines 24-28), add **between** `clipboard_exe` and `log_dir`:
  ```yaml
    controller_exe: "interceptors/peripheral_storage/Controller/bin/Debug/net10.0-windows/win-x64/UsbDlpController.exe"
  ```
  Note the `win-x64\` segment is mandatory because `Controller.csproj` sets `<RuntimeIdentifier>win-x64</RuntimeIdentifier>` (line 10 of Controller.csproj). `ClipboardInterceptor.csproj` does NOT set RuntimeIdentifier, so its existing path string at line 27 of config.yaml has no `win-x64\` — leave it.

- `orchestrator/config.py` — insert into `OrchestratorConfig` dataclass (after the existing `clipboard_exe: str` at line 24):
  ```python
      controller_exe: str
  ```
  And into `load_config()` return args (after line 62, where `clipboard_exe=paths.get(...)` is):
  ```python
          controller_exe=paths.get(
              "controller_exe",
              "interceptors/peripheral_storage/Controller/bin/Debug/net10.0-windows/win-x64/UsbDlpController.exe",
          ),
  ```

- `scripts/harness/conftest.py` — **no edits needed.** `paths.get(..., default)` on line 62 supplies the same default if the harness's test config omits the key. Existing pytests load cleanly.

**Validates:** `python -c "from orchestrator.config import load_config; c = load_config(); print(c.controller_exe)"` prints the default path (or the override if set in yaml).

### IT-C3. (Empty — folded into IT-C1)

Originally a separate task for the file-logger wiring. The plan agent's design folds this into `Supervisor.__init__`, so IT-C1 covers it. Numbering preserved to keep cross-references in PR descriptions stable.

### IT-C4. Wire Supervisor into `_run_foreground`

**Goal:** `python -m orchestrator --foreground` spawns and supervises the three children; Ctrl+C tears them down before the orchestrator's pipes go down.

Edits to `orchestrator/__main__.py` (line numbers reference the current file):

- Add import near the existing imports (after line 19):
  ```python
  from orchestrator.supervisor import Supervisor, build_default_specs
  ```

- After **line 122** (`t.start()`, where `t` is the pipe-server thread), insert:
  ```python
  repo_root = Path(__file__).parent.parent
  supervisor = Supervisor(
      config,
      repo_root=repo_root,
      specs=build_default_specs(config, repo_root),
  )
  supervisor.start_all()
  log.info("Supervisor started; supervising %d children.", len(supervisor.status_snapshot()))
  ```
  **Why this position:** ConfigWatcher, CtlServer thread, and pipe-server thread are already running. Both `data_pipe` and `ctl_pipe` are reachable. Children connecting at startup find them immediately (no startup race).

- Replace the existing `except KeyboardInterrupt` block (lines 127-132) so `supervisor.stop_all()` runs **first**:
  ```python
  except KeyboardInterrupt:
      log.info("Ctrl+C received, shutting down...")
      supervisor.stop_all()       # FIRST — Controller releases alive mutex while pipes still up
      server.stop()
      ctl_server.stop()
      t.join(timeout=5.0)
      ctl_thread.join(timeout=5.0)
  ```

- Update the `finally` block (lines 133-137) to call `supervisor.stop_all()` defensively (idempotent — D7), guarded against `NameError` if Supervisor construction itself raised:
  ```python
  finally:
      if "supervisor" in locals():
          supervisor.stop_all()    # idempotent; no-op if already stopped
      config_watcher.stop()
      dispatcher.shutdown(wait=True)
      pm.stop()
      log.info("Orchestrator stopped cleanly.")
  ```

**Validates:** see *Verification* below. The end-to-end smoke is what proves this task.

### IT-C5. (Optional) Harness lifecycle smoke test

**Goal:** one cheap automated regression so a future refactor of `start_all/stop_all` cannot silently break the lifecycle.

Caveat noted by the plan agent: a vanilla `python.exe -c "import time; time.sleep(60)"` child does NOT have a default `SIGBREAK` handler — Python on Windows will exit on `CTRL_BREAK_EVENT` with no chance to handle it, but the test cannot verify the *graceful* path that way. The test deliberately exercises only the **terminate-fallback** path: child has no BREAK handler, so it survives the BREAK, hits the grace timeout, and is killed via `proc.terminate()`. This is enough to verify spawn / state-tracking / fallback-kill / thread-joins. The graceful-BREAK path is verified manually (see step 4 of *Verification*).

New file `scripts/harness/test_supervisor.py`:

```python
import sys
import time
from pathlib import Path

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.supervisor import ChildSpec, Supervisor


def _minimal_config(tmp_path) -> OrchestratorConfig:
    # Just enough fields for Supervisor to read.
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


def test_supervisor_lifecycle(tmp_path):
    spec = ChildSpec(
        name="stub",
        exe=Path(sys.executable),
        args=["-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        grace_seconds=1.5,            # short — we expect the terminate fallback
        critical_terminate=False,
    )
    sup = Supervisor(
        _minimal_config(tmp_path),
        repo_root=tmp_path,
        specs=[spec],
        log_dir=tmp_path / "logs",
    )
    sup.start_all()
    time.sleep(0.5)
    assert sup.status_snapshot()["stub"]["alive"] is True

    sup.stop_all()
    assert sup.status_snapshot()["stub"]["alive"] is False

    # Per-child log file exists and has at least a header line from RotatingFileHandler creation.
    assert (tmp_path / "logs" / "supervisor-stub.log").exists()
```

**Validates:** runs in <5 s in pytest. No C# binaries required, so the test is safe on a fresh checkout.

## Verification

### Build commands (verified to be the correct invocations)

From **Visual Studio 2026 Developer PowerShell** at repo root:
```powershell
dotnet build src\ClipboardInterceptor\ClipboardInterceptor.csproj
dotnet build interceptors\peripheral_storage\Controller\Controller.csproj
# Payload.dll dependency — build once if not present (per project memory: msbuild for .vcxproj):
& "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe" `
    interceptors\peripheral_storage\Payload\Payload.vcxproj /p:Configuration=Debug /p:Platform=x64
```

From **normal PowerShell** with `.venv` active:
```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest scripts\harness\ -v          # 9 Phase B tests still pass + 1 new test_supervisor
python -m orchestrator --foreground --config config.yaml
```

### End-to-end smoke

Run in this order. Each step is a `Done when:` checkpoint.

1. **Cold spawn.** Start `python -m orchestrator --foreground` in a fresh shell (admin Developer PowerShell preferred because Controller needs `SeDebugPrivilege` to inject into `explorer.exe`).
   - **Done when:** the orchestrator log contains three `Supervisor: spawned <name> pid=<N>` lines. `Get-Process mitmdump,ClipboardInterceptor,UsbDlpController -ErrorAction SilentlyContinue` lists three PIDs.
   - `%PROGRAMDATA%\DLP\logs\supervisor-controller.log` has `[Controller] Alive mutex created.` and `[Controller] Subscribing to ctl pipe: \\.\pipe\dlp_agent_ctl`.

2. **mitmdump crash respawn.** Kill `mitmdump.exe` via Task Manager.
   - **Done when:** orchestrator log shows `Supervisor: mitmdump crashed (exit=<...>); restarting (1/3 in last 60s)` within ~1 s. `Get-Process mitmdump` shows a new PID.

3. **Controller crash respawn (hot path — DLL re-injection).** Kill `UsbDlpController.exe`. **Pre-condition:** `tasklist /m Payload.dll` shows Payload loaded in `explorer.exe` before the kill.
   - **Done when:** `Get-Process UsbDlpController` shows a new PID; `supervisor-controller.log` shows fresh `Alive mutex created` and `Injected Payload.dll into explorer.exe`.

4. **Graceful Ctrl+C shutdown (the hooks-deactivate test).** Press Ctrl+C in the orchestrator's terminal. Watch `supervisor-controller.log` while doing so.
   - **Done when (in chronological order):**
     1. Orchestrator log: `Ctrl+C received, shutting down...`
     2. Orchestrator log: `Supervisor: sent CTRL_BREAK_EVENT to controller pid=<N>` (and likewise for mitmdump, clipboard).
     3. `supervisor-controller.log`: `[Controller] Releasing mutex — hooks deactivating...` within 1 s of step 2. **This is the load-bearing assertion** — it proves `CTRL_BREAK_EVENT` invoked Controller's existing `Console.CancelKeyPress` handler, which then ran the mutex-release path at `Controller/Program.cs:190` BEFORE the WMI-watcher-disposal that can stall 15-30 s.
     4. `supervisor-controller.log`: `[Controller] Alive mutex released.` then `[Controller] Shutting down...`.
     5. `supervisor-clipboard.log`: `[DLP] Shutting down...`.
     6. Orchestrator log: `Supervisor: <name> exited gracefully (code=0)` for all three.
     7. All three children gone from `Get-Process`; orchestrator process exits.
   - **Negative:** if `[Controller] Releasing mutex` does NOT appear before the orchestrator log says `terminate` for controller, R1 has fired — investigate before continuing.

5. **Restart-cap give-up.** Kill `UsbDlpController.exe` four times in quick succession (within 60 s total).
   - **Done when:** after the 4th kill, orchestrator log shows `Supervisor: controller exceeded restart cap (3 crashes in 60s, exit=<...>); giving up on this child. Other children remain supervised.` No new controller PID appears. `Get-Process mitmdump,ClipboardInterceptor` still show their PIDs.

6. **Stable-uptime reset.** Kill controller once, wait 65 s (more than `stable_uptime_reset_seconds=60`), kill it again.
   - **Done when:** orchestrator log on the second kill shows `restarting (1/3 in last 60s)`, NOT `(2/3 ...)` — confirms the `crash_history.clear()` fired after the stable window.

7. **Failed exe path (fail-loudly).** Edit `config.yaml` `paths.controller_exe` to `"nonexistent\\foo.exe"`, restart the orchestrator.
   - **Done when:** orchestrator exits with `FileNotFoundError: Supervisor: child 'controller' exe does not exist at <absolute resolved path>`. **No child is spawned** (the validation runs in `Supervisor.__init__`, before `start_all`). Restore the yaml after.

8. **Phase B regressions.** Re-run the Phase B verification table — ctl-pipe selective-skip, browser addon hot reload, controller config push. All should still pass because Phase C only adds wiring; no Phase B code path is touched.

### Automated tests

- `python -m pytest scripts\harness\ -v` — 9 Phase B tests + 1 new `test_supervisor.py` case (the lifecycle smoke from IT-C5). All pass.
- `dotnet test src\AgentCore.Tests\AgentCore.Tests.csproj` — no Phase C changes to .NET code, so the existing 10 cases (2 PipeAgentCore + 5 ConfigLocator + 3 PipeNameHelper) pass unchanged.

## Risks

**R1 — Forced termination of Controller leaves hooks live in `explorer.exe`.** *Trigger:* Controller does not exit within its 10 s grace window (e.g., the WMI watcher is wedged, or a debugger is attached). *Mitigation:* the supervisor logs at `CRITICAL` (D10) so the failure is loud in `dlp-agent.log`. Recovery: restart `explorer.exe` from Task Manager — that re-reads the DllNotificationCallbacks and unloads Payload because the alive mutex is no longer signalled. *Why 10 s is enough in practice:* `Program.cs:190` releases the mutex BEFORE `processMonitor.Dispose()` runs, so even if the disposal stalls, the hooks have already deactivated. The grace window only has to span "BREAK received → line 191 executes," which is sub-millisecond. The 10 s budget is overhead for the rest of teardown.

**R2 — mitmproxy's `done()` doesn't fire on Windows BREAK.** Known upstream issue. *Mitigation:* benign — the addon's `CtlPipeSubscriber` thread is `daemon=True` (`ctl_pipe_subscriber.py:62`), so process exit reaps it regardless of whether `done()` runs. We don't depend on `done()` for correctness; we only get the addon's "stop subscriber thread" niceness when it fires.

**R3 — Orphaned children if the orchestrator is `kill -9`'d (Task Manager → End Task, force).** Windows has no automatic process-tree cleanup. *Phase C disposition:* accept. Leave a TODO comment near `_spawn` referencing Win32 Job Objects (`CreateJobObject` + `AssignProcessToJobObject` with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) for Phase E to address alongside the LocalSystem service work. In foreground/dev usage, "End Task" on the orchestrator is rare — operators are expected to use Ctrl+C.

**R4 — Pipe-server bind race vs. early child connect.** *Scenario:* child starts faster than the pipe server's `CreateNamedPipe` first call returns; child's `CreateFile` against the pipe gets `ERROR_FILE_NOT_FOUND`. *Mitigation:* every C# client (`CtlPipeSubscriber.cs`, `OrchestratorClient.cs`) already has connect-retry with exponential backoff (250 ms → 4 s cap), inherited from Phase B fix #1. The Python addon's `pipe_client` does similar. Sequencing in IT-C4 puts both server threads' `.start()` BEFORE `supervisor.start_all()`, but Python thread scheduling is not deterministic — the client-side retry is the actual safety net, not the ordering.

**R5 — `propagate=False` means children's output is NOT on the orchestrator's foreground console.** *Trade-off:* keeps `dlp-agent.log` focused on orchestrator events. *Workaround for dev:* a second PowerShell with `Get-Content -Wait %PROGRAMDATA%\DLP\logs\supervisor-controller.log` for live tailing. If the operator strongly prefers everything on one screen, future work can drop `propagate=False` and accept duplication; not done in Phase C because it dilutes `dlp-agent.log`'s usefulness.

**R6 — `mitmdump.exe` from `.venv\Scripts\` re-binds proxy port 8080 on respawn.** *Scenario:* crashed mitmdump released the socket; respawn rebinds. *Risk:* if the old mitmdump was kill -9'd by us (R1 path applied to mitmdump), the socket may linger in TIME_WAIT and bind fails. *Mitigation:* benign in practice because `proc.terminate()` is only used during `stop_all`, where no respawn follows. Crashes (exit ≠ 0) are user-action kills where the socket is already released by the OS. If the bind ever does fail, the child crashes on startup and the watcher counts it against the restart cap — same fallback as any other crash.

**R7 — Phase E re-architecture.** Phase E moves the orchestrator into a `LocalSystem` service and spawns children into user sessions via `CreateProcessAsUser`. The supervisor API designed here (`start_all` / `stop_all` / `status_snapshot` / `build_default_specs`) is meant to survive that transition — Phase E adds a per-session spawn helper that produces `ChildSpec` lists keyed by `(session_id, name)` and a slightly extended Supervisor that maintains a `dict[tuple[int, str], _ChildState]`. Nothing in Phase C should preclude that — no module-scope state, no hard-coded child counts.

---

## Implementation notes (additions made during IT-C1…IT-C5)

These are retroactive locks on choices that emerged during implementation, plus one non-actionable build-time warning that surfaced in verification.

**D11 — `DLP_SUPERVISOR_DISABLED=1` env-var opt-out.** Without an opt-out, every harness-spawned orchestrator tries to spawn the three real children. The harness sets `paths.clipboard_exe: ""`, which `build_default_specs` resolves to the repo root (not a file) → `Supervisor.__init__` raises `FileNotFoundError` → all 9 Phase A/B tests fail at orchestrator startup. The env-var gate in `_run_foreground` skips Supervisor entirely; `scripts/harness/conftest.py` sets it for every spawned orchestrator. Production never sets it; behavior under `python -m orchestrator --foreground` is unchanged. *Why an env var rather than a config flag:* matches existing harness affordances (`DLP_TEST_SLOW_MS`, `DLP_CONFIG_PATH`); no schema change to `OrchestratorConfig`.

**Build-time `NU1900` warnings (not actionable).** `verify-install.ps1`'s `dotnet publish` step emits `warning NU1900: Error occurred while getting package vulnerability data: Unable to load the service index for source https://api.nuget.org/v3/index.json.` Cause: the build machine couldn't reach `api.nuget.org` during build, so the vulnerability-database lookup was skipped. **Build still succeeds.** This is an operator-environment issue (proxy, offline run, transient outage), not a code issue. No fix in this plan. Mitigation if it becomes noisy: `dotnet build /p:NuGetAudit=false` opts out of the audit step entirely.

---

## Post-implementation fix #1 — Data-pipe ACL for cross-integrity TransferAgent access

### Context

End-to-end smoke verified the plan's *Verification* checklist successfully (cold spawn, crash respawns, Ctrl+C hooks-deactivate, restart-cap, stable-uptime reset, fail-loud). One symptom surfaced afterwards that the plan did not anticipate: **TransferAgent transfers BLOCK every file with note `"Orchestrator error: Access to path is denied."`** Triggered when the orchestrator is started in an **admin** Developer PowerShell (which is now required for Phase C — supervised Controller needs `SeDebugPrivilege` to `LoadLibrary`-inject `Payload.dll` into `explorer.exe`).

### Root cause

When the orchestrator runs elevated (high mandatory integrity level), `win32pipe.CreateNamedPipe` with `lpSecurityAttributes=None` (see `orchestrator/server.py:72-81`) accepts the **default named-pipe DACL**: full control to `NT AUTHORITY\SYSTEM`, `BUILTIN\Administrators`, and `CREATOR OWNER`; **read-only** access for `Everyone` and the anonymous logon. TransferAgent is launched by ShellExtension which is hosted in `explorer.exe` — a **medium-integrity** user-session process. TransferAgent's `NamedPipeClientStream(".", PipeName, PipeDirection.InOut, …)` (`OrchestratorClient.cs:87-88`) requests `GENERIC_READ | GENERIC_WRITE`. The default DACL gives the user `READ` but not `WRITE`, so `ConnectAsync` fails with `ERROR_ACCESS_DENIED` → .NET raises `UnauthorizedAccessException("Access to path is denied.")` → the catch-all at `OrchestratorClient.cs:128-133` wraps the message as `"Orchestrator error: Access to path is denied."` and returns `Allowed=false`. The TransferForm renders this verbatim in the BLOCK row's Note column.

**Why this was invisible before Phase C.** Pre-Phase-C, the orchestrator typically ran in a normal (non-elevated) shell while the operator manually launched Controller in a separate admin shell. Both orchestrator and TransferAgent were medium integrity; `CREATOR OWNER` in the default DACL granted the orchestrator's pipe full access to its own session at the same integrity, and TransferAgent at the same integrity could open it for read+write via the same SID resolution. Once the supervisor brought Controller under the orchestrator's lifecycle (Phase C), the orchestrator *must* run elevated, splitting the integrity levels and breaking TransferAgent.

`integration-plan2.md` line 139 (Phase E scope note) pre-flagged this exact issue: *"Named-pipe security descriptor: `Authenticated Users` granted `FILE_GENERIC_READ | FILE_GENERIC_WRITE` on data pipe (old plan Phase 4 step 3) — required so user-session interceptors can open the pipe."* Phase C is now advancing the data-pipe-ACL portion because Phase C forced the integrity-level split. (`ctl_pipe` stays on the default DACL — see *Scope* below.)

### Fix

Build a `SECURITY_ATTRIBUTES` once in `PipeServer.__init__` and pass it as the last argument to every `CreateNamedPipe` call in `_accept_loop`. The DACL grants:

| SID | Access mask |
|---|---|
| `NT AUTHORITY\SYSTEM` (`WinLocalSystemSid`) | `FILE_ALL_ACCESS` |
| `BUILTIN\Administrators` (`WinBuiltinAdministratorsSid`) | `FILE_ALL_ACCESS` |
| `NT AUTHORITY\Authenticated Users` (`WinAuthenticatedUserSid`) | `FILE_GENERIC_READ \| FILE_GENERIC_WRITE` |

Concrete shape (insert into `orchestrator/server.py`, called from `PipeServer.__init__`, stored as `self._pipe_sa`):

```python
import ntsecuritycon
import win32security

def _build_pipe_sa():
    """SECURITY_ATTRIBUTES granting Authenticated Users RW on the named pipe.

    Needed when the orchestrator runs elevated and clients (notably
    TransferAgent, launched at medium integrity from explorer.exe) need
    GENERIC_READ | GENERIC_WRITE on the pipe. The default DACL gives
    Everyone only READ, which fails .NET's NamedPipeClientStream(InOut)
    with UnauthorizedAccessException("Access to path is denied.").
    """
    dacl = win32security.ACL()
    sys_sid = win32security.CreateWellKnownSid(
        win32security.WinLocalSystemSid, None)
    admins_sid = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid, None)
    auth_users_sid = win32security.CreateWellKnownSid(
        win32security.WinAuthenticatedUserSid, None)
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sys_sid)
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, admins_sid)
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE,
        auth_users_sid,
    )
    sd = win32security.SECURITY_DESCRIPTOR()
    sd.SetSecurityDescriptorDacl(1, dacl, 0)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    return sa
```

Wire it once (not per accept) — Win32 only reads the descriptor bytes during the `CreateNamedPipe` call, and `PySECURITY_ATTRIBUTES` is reference-stable. Pass `self._pipe_sa` as the 8th positional arg to `win32pipe.CreateNamedPipe` (currently `None` at `server.py:80`).

No change to `orchestrator/ctl_server.py:147-154` (the ctl-pipe `CreateNamedPipe` call). See *Scope* below.

### Scope

- **data_pipe only.** All Phase C ctl-pipe subscribers (Controller, ClipboardInterceptor, browser addon) are supervised children that inherit the orchestrator's elevation. TransferAgent does NOT subscribe to the ctl-pipe (Phase B decision #9 — per-file lifecycle, one-shot read). So the ctl_pipe never gets a cross-integrity client today.
- **Phase E will tighten ctl_pipe** to `BUILTIN\Administrators`-only (already a TODO at `ctl_server.py:34-36`) AND simultaneously add cross-session Authenticated Users access for the data_pipe in service-mode. The Phase E work overrides this fix's data-pipe DACL only if it broadens it (it won't — Authenticated Users is the same SID Phase E needs).
- **No env-var gate.** The new DACL is strictly more permissive than the default and breaks nothing for existing clients. Always-on is safer than opt-in.

### Critical files

**Edits**
- `D:\Code\GithubPublishEndpointDLP\orchestrator\server.py` — add `_build_pipe_sa()` (~30 LOC), call from `PipeServer.__init__`, pass `self._pipe_sa` as the 8th arg to `win32pipe.CreateNamedPipe` at `server.py:72-81`. Add the two new imports (`ntsecuritycon`, `win32security`) at the top.

**Not changed**
- `orchestrator/ctl_server.py` — see *Scope*.
- `OrchestratorClient.cs`, `TransferForm.cs`, `Program.cs` (TransferAgent) — the C# client side needs zero change; .NET asks for `GENERIC_READ | GENERIC_WRITE` and the new DACL now grants it.
- `config.yaml` — no new fields. The DACL is hardcoded; making it configurable would just expose a footgun.

### Verification

1. **Reproduction (pre-fix baseline).** Orchestrator running in admin Developer PowerShell. Right-click any clean file → "Transfer to USB (DLP Protected)". Expected (pre-fix): TransferForm shows BLOCK with note `"Orchestrator error: Access to path is denied."`. Confirms the bug is present.
2. **Apply the edit.** Restart the orchestrator (still in admin PowerShell).
3. **Smoke (post-fix).** Trigger the same transfer. Expected: a clean file shows TRANSFERRED; orchestrator's `dlp-agent.log` shows `recv req=... channel=peripheral_storage kind=file size=...` and `ALLOW`. No `Access to path is denied` text appears anywhere.
4. **Negative path — file with PII.** Pick a file that should BLOCK (e.g., a `.txt` containing a Vietnamese CCCD). Expected: TransferForm row shows BLOCKED with a real policy-derived reason in the Note column (NOT the access-denied string).
5. **Phase B regressions.** Re-run `python -m pytest scripts\harness\ -v`. Expected: 12/12 pass. The harness spawns its orchestrator in the same shell as pytest (typically non-elevated); the broader DACL is a no-op there because the orchestrator and harness client are already same-integrity.
6. **`dotnet test src\AgentCore.Tests\AgentCore.Tests.csproj`** — no C# changes; expect 10/10 pass unchanged.

### Risks

**RX1 — Authenticated Users includes every interactively-logged-on local user.** *Implication:* on a multi-user machine, a second logged-on user's processes can also open the data_pipe and submit analysis requests. *Acceptable for Phase C* (foreground/dev mode, single-operator typical). Phase E moves to the LocalSystem service model and will tighten via per-session pipe instances (one orchestrator per session via `WTSQueryUserToken` + `CreateProcessAsUser`).

**RX2 — The DACL doesn't address mandatory-integrity policy.** Named pipes are not UIPI-protected the way GUI windows and clipboard are; DACL is the entire access check. Verified empirically by the fix working from medium-integrity TransferAgent.

**RX3 — `win32security` import adds a small startup cost.** Pywin32 is already a dependency (`ctl_server.py`, `server.py`, `config_watcher.py` all use it). The import cost is ~1 ms, irrelevant.

**RX4 — Forgetting to apply this in Phase E.** The Phase E plan needs to copy this `_build_pipe_sa` shape (or equivalent) into the service-mode pipe creation path. Cross-reference comment in the source will keep this discoverable.

---

## Post-implementation fix #2 — TransferAgent UI: make BLOCK reasons copyable

### Context

When TransferAgent rejects a file, the Note column carries the diagnostic — sometimes essential (e.g., the fix #1 access-denied message; policy violation IDs; analyzer timeout details). The user reports: *"i cant [copy], it makes me type out the error by hand."* The `_listView` in `TransferForm.cs:66-79` is a stock WinForms `ListView` with no copy affordance — selecting a row doesn't expose cell text to the clipboard, and there's no context menu.

### Fix

Two affordances, both standard WinForms patterns, that share one helper:

1. **Keyboard:** Ctrl+A selects all rows; Ctrl+C copies selected rows to the clipboard as TSV (tab-separated columns, CRLF-terminated rows — the format Excel and most editors paste cleanly).
2. **Context menu:** right-click on the ListView shows a `ContextMenuStrip` with `Copy` (selected rows → clipboard) and `Copy Note` (just the Note column of the selected rows). `Copy Note` is the explicitly-asked-for path for error text; `Copy` is the general case.

Implementation (inserted into `TransferForm.cs`, near the `_listView` construction at line 66-79):

```csharp
// Ctrl+A selects all; Ctrl+C copies selected rows as TSV. Right-click → context menu.
_listView.KeyDown += (_, e) =>
{
    if (e.Control && e.KeyCode == Keys.A)
    {
        foreach (ListViewItem it in _listView.Items) it.Selected = true;
        e.Handled = e.SuppressKeyPress = true;
    }
    else if (e.Control && e.KeyCode == Keys.C)
    {
        CopySelectedRowsToClipboard(noteOnly: false);
        e.Handled = e.SuppressKeyPress = true;
    }
};

var ctx = new ContextMenuStrip();
ctx.Items.Add("Copy",      null, (_, _) => CopySelectedRowsToClipboard(noteOnly: false));
ctx.Items.Add("Copy Note", null, (_, _) => CopySelectedRowsToClipboard(noteOnly: true));
_listView.ContextMenuStrip = ctx;
```

Helper (new method on `TransferForm`):

```csharp
private void CopySelectedRowsToClipboard(bool noteOnly)
{
    if (_listView.SelectedItems.Count == 0) return;
    var sb = new System.Text.StringBuilder();
    foreach (ListViewItem it in _listView.SelectedItems)
    {
        if (noteOnly)
        {
            // SubItems[3] is the Note column (File, Status, Size, Note).
            sb.AppendLine(it.SubItems.Count > 3 ? it.SubItems[3].Text : "");
        }
        else
        {
            var cells = new string[it.SubItems.Count];
            for (int i = 0; i < it.SubItems.Count; i++) cells[i] = it.SubItems[i].Text;
            sb.AppendLine(string.Join("\t", cells));
        }
    }
    try { Clipboard.SetText(sb.ToString()); }
    catch (System.Runtime.InteropServices.ExternalException)
    {
        // Clipboard occasionally fails under OLE contention — silently ignore;
        // user can retry. A MessageBox here would be more annoying than the failure.
    }
}
```

Add `using System.Text;` if not already pulled in transitively (it likely is via existing `using System.Security.Cryptography;` + `Encoding.UTF8` usage in `OrchestratorClient.cs`, but verify in the file's existing using-block).

### Critical files

**Edit only**
- `D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\TransferAgent\TransferForm.cs` — append the two event hookups inside the constructor (after the `_listView.Columns.Add(...)` calls at line 76-79), and add the `CopySelectedRowsToClipboard` private method near `OnCloseClick` / `FormatSize` (line 344+).

**Not changed**
- `OrchestratorClient.cs` — the error string source. Untouched (changing the message format would break fix #1's verification grep and provide no UX win).
- `Program.cs` (TransferAgent) — unchanged.

### Verification

1. Trigger a transfer that produces at least one BLOCK row (a `.txt` containing a CCCD against the strict fixture policy is the easiest path).
2. **Keyboard:** click a BLOCK row, press Ctrl+C, paste into Notepad. Expect one tab-separated line: `<filename>\t<status>\t<size>\t<note>`.
3. **Select-all:** press Ctrl+A, then Ctrl+C, paste into Notepad. Expect one line per row.
4. **Context menu — Copy Note:** right-click a BLOCK row → Copy Note → paste. Expect just the policy/error string (no filename/status/size cells, no leading tabs).
5. **Multi-row Copy Note:** select two BLOCK rows (Shift-click), right-click → Copy Note → paste. Expect two lines, one note per row.
6. **No regression:** Cancel/Close buttons still work; existing tabs (the ProgressBar→ListView swap on done) still works.

### Risks

**RY1 — Clipboard contention with active OLE clients (e.g., a parallel Word paste).** `Clipboard.SetText` can throw `ExternalException` if another process holds the clipboard. Wrapped silently per the standard WinForms pattern; user can retry. Not worth a MessageBox.

**RY2 — Future column reordering.** The `noteOnly` branch hardcodes `SubItems[3]`. If columns are reordered in `TransferForm.cs:76-79`, this index moves. *Mitigation:* the comment in the helper names the assumption explicitly; a future column-rename refactor will see it. Not worth a column-name lookup table for four columns.

**RY3 — Accessibility.** Stock WinForms `ListView` + `ContextMenuStrip` + keyboard handlers are screen-reader-compatible by default. No regression.
