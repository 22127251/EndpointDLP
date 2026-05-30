  # DLP Endpoint Agent — Integration Plan

## Context

The project has three pieces that already work in isolation on Windows 11 x64 / Python 3.13:

- **Analyzer** (`analyzer/`) — `DLPEngine` at `analyzer/engine.py:87`, file extraction at `analyzer/extractor.py`, YAML policies at `analyzer/policies.yaml`. Plain-text and tabular analysis modes; returns `Allow` / `allow_log` / `Block`.
- **Browser interceptor** — mitmproxy addon at `addon.py` (root) with a Windows named-pipe client at `pipe_client.py`. Sends upload metadata + temp file path to a pipe server, reads back `ALLOW`/`BLOCK`. Currently tested against `stub_consumer.py`.
- **Clipboard interceptor** — .NET project at `src/ClipboardInterceptor`. `ClipboardMonitor.cs` (WM_CLIPBOARDUPDATE on an STA message-only window) + `ClipboardInterceptorService.cs` (per-copy cancellation via `_currentCts` + stale-decision guard via `_currentAnalysisId`). Wired to a `StubAgentCore` that prompts at the console.

None of these are integrated. `src/QueueManager` is a compiled-only dead prototype (no source — abandoned). `src/AgentCore/StubAgentCore.cs` is a placeholder.

**Goal:** build a Python orchestrator that hosts the analyzer engine, exposes a named-pipe server for both interceptors, supervises them as child processes, and runs as a Windows Service with auto-start and admin-protected lifecycle. The endpoint must be usable for development (foreground mode) and as a production-like service on the testing device. Plan is phased so each step is independently testable.

---

## Locked design decisions

| # | Decision | Why |
|---|---|---|
| 1 | Single Python orchestrator, multi-process (launches child interceptors) | Matches user's intent; avoids duplicating the engine; single point of policy hot-reload |
| 2 | Windows Service under `LocalSystem`, interceptors spawned into active user session via `CreateProcessAsUser` | Clipboard events + user proxy reg keys are per-session; LocalSystem can't see them directly |
| 3 | IPC via Windows named pipes: `\\.\pipe\dlp_agent` (data) + `\\.\pipe\dlp_agent_ctl` (admin) | Matches existing `pipe_client.py`; stays local-only; easy ACLs |
| 4 | Unified wire protocol: JSON request with `channel` field, plain-text `ALLOW`/`BLOCK` response, one connection per request | Preserves the existing browser addon shape with minimal changes |
| 5 | `pywin32` for both pipe server and service framework | Already a transitive dep; one Windows API surface |
| 6 | Orchestrator has `--foreground`, `--install`, `--uninstall`, `--service` subcommands | Single entrypoint owns the install/uninstall lifecycle (cert + proxy + service reg) |
| 7 | Policy hot-reload via `watchdog` file watcher on `analyzer/policies.yaml`; atomic engine swap; zero in-flight request loss | User wants near-zero downtime on policy change |
| 8 | Per-channel `ThreadPoolExecutor`s (defaults: clipboard=2, browser=3) | Guarantees clipboard is never starved by a slow browser upload; configurable to fit RAM/CPU budget |
| 9 | Child restart: max 3 crashes per 60 s window, then back off; both values configurable | User requested configurable retry with default 3 |
| 10 | Clipboard text cap: 1 MB; anything larger → fail-closed with log entry | Huge pastes are both slow to analyze and suspicious |
| 11 | Fail-behavior on IPC outage: configurable per channel; default **fail-closed** for both | DLP posture: when in doubt, block |
| 12 | Logging to `%PROGRAMDATA%\DLP\logs\` (rotating file) + console mirror in foreground mode | Standard Windows service convention |
| 13 | Clipboard supersession: client-side cancel (existing) + orchestrator-side coarse check before writing response; **global scope** (one active clipboard analysis at a time) | Drops stale decisions without invasive changes to `DLPEngine` |
| 14 | Chunking concept dropped | RE2 + Aho-Corasick over 1 MB is <100 ms; chunking only existed to work around Presidio's slow NLP pass |
| 15 | Peripheral channel stays a stub | Out of scope for this iteration |
| 16 | Pipe ACL: `Authenticated Users` R/W on data pipe; `Administrators` only on control pipe | Good enough for Phase-1 testing; tighter process-identity checks can layer on later |

---

## Target directory layout (end state)

```
orchestrator/                      NEW top-level package
  __main__.py                      subcommand dispatch (--foreground/--install/--uninstall/--service)
  config.py                        orchestrator config loader
  server.py                        pipe listener (multi-instance), framing
  dispatcher.py                    per-channel ThreadPoolExecutor + supersession tracker
  policy_manager.py                DLPEngine lifecycle + atomic hot-swap (watchdog)
  supervisor.py                    child process spawn / watch / restart loop
  session.py                       WTS session-change notifications + CreateProcessAsUser
  installer.py                     --install/--uninstall: cert, proxy, service reg
  service.py                       pywin32 win32serviceutil.ServiceFramework wrapper
  logging_setup.py                 rotating file + optional console
  ctl.py                           dlp-ctl admin CLI (reload, status, tail)

interceptors/
  browser/                         moved from repo root
    addon.py                       + new unified wire format
    pipe_client.py                 largely unchanged
    config.py                      browser-addon-specific (extensions, upload keywords)
    config.yaml
  clipboard/
    PipeAgentCore.cs               NEW — replaces StubAgentCore, uses NamedPipeClientStream
    (src/ClipboardInterceptor/ stays put; only Program.cs and AgentCore/ are touched)

analyzer/                          UNCHANGED
orchestrator.yaml                  NEW top-level config
```

Deletions:
- `stub_consumer.py` (superseded by real orchestrator)
- Old root-level `config.py`, `config.yaml`, `pipe_client.py`, `addon.py` — moved to `interceptors/browser/`
- `src/QueueManager/` (abandoned prototype with no source)
- `src/AgentCore/StubAgentCore.cs` (replaced by `PipeAgentCore.cs`)

---

## Wire protocol

**Data pipe** (`\\.\pipe\dlp_agent`): one request per connection; client sends a single JSON message, reads a single UTF-8 string response, closes.

```json
// Request
{
  "channel": "clipboard" | "browser",
  "kind": "text" | "file",
  "text": "...",           // when kind=text
  "file_path": "...",      // when kind=file; orchestrator owns cleanup
  "metadata": {            // optional, free-form per channel
    "url": "...",
    "filename": "...",
    "size_bytes": 1234,
    "timestamp": "2026-04-19T10:00:00Z"
  }
}
```

```
// Response
ALLOW
```

or

```
BLOCK
```

Mapping from analyzer action → wire response:
- `allow` → `ALLOW`
- `allow_log` → `ALLOW` + log entry with full violation list (interceptor never sees the distinction)
- `block` → `BLOCK` + log entry

**Control pipe** (`\\.\pipe\dlp_agent_ctl`): same framing, command strings like `{"cmd":"reload"}` / `{"cmd":"status"}`. Admins-only ACL.

---

## Orchestrator config (`orchestrator.yaml`)

```yaml
data_pipe:   "\\\\.\\pipe\\dlp_agent"
ctl_pipe:    "\\\\.\\pipe\\dlp_agent_ctl"

pools:
  clipboard_workers: 2
  browser_workers:   3
  pipe_listeners:    4

limits:
  max_clipboard_bytes: 1048576       # 1 MB
  max_file_bytes:      104857600     # 100 MB (browser)

supervisor:
  max_restarts: 3
  restart_window_seconds: 60
  stable_uptime_reset_seconds: 60

paths:
  mitmdump_exe: ""                   # auto-detect in venv if empty
  addon_script: "interceptors/browser/addon.py"
  clipboard_exe: "src/ClipboardInterceptor/bin/Debug/net10.0-windows/ClipboardInterceptor.exe"
  log_dir: ""                        # empty → %PROGRAMDATA%\DLP\logs

proxy:
  listen_port: 8080
  bypass: "localhost;127.0.0.1;<local>"

policies_file: "analyzer/policies.yaml"
```

---

## Phased implementation

Each phase ends in a testable state. **Do not advance before the phase-end test passes.**

### Phase 0 — Scaffolding (no behavior change)

Goal: directories and empty modules in place; existing flows still runnable.

Steps:
1. Create empty `orchestrator/` with stub `__init__.py`, `__main__.py`, and the other module files (empty or with only docstrings).
2. Create `interceptors/browser/`. `git mv` `addon.py`, `pipe_client.py`, `config.py`, `config.yaml` into it.
3. Update `addon.py`'s imports: `import pipe_client` / `from config import ...` — both files sit next to it, so the imports stay the same; just the `mitmdump` launch command changes to `mitmdump -s interceptors/browser/addon.py` (working directory = `interceptors/browser/`).
4. Draft `orchestrator.yaml` at repo root with the schema above. No code reads it yet.
5. Delete `stub_consumer.py`, `src/QueueManager/`.

Verify:
- `python analyzer/test_cli.py --text "..." --channel clipboard` — still works.
- `cd interceptors/browser && mitmdump -s addon.py` — still loads the addon (but with no pipe server running).

### Phase 1 — Single-threaded orchestrator, foreground mode, end-to-end

Goal: `python -m orchestrator --foreground`, manually launched mitmdump, manually launched clipboard interceptor — all three running in three shells, real decisions flowing end-to-end.

Steps:
1. `orchestrator/logging_setup.py` — `RotatingFileHandler` under `%PROGRAMDATA%\DLP\logs\dlp-agent.log` + `StreamHandler` to stdout when `--foreground`.
2. `orchestrator/config.py` — dataclass + YAML loader mirroring `analyzer/policy.py:load_policies` style.
3. `orchestrator/policy_manager.py`:
   - `PolicyManager.get_engine()` returns the current `DLPEngine` (imported from `analyzer/engine.py`).
   - `PolicyManager.analyze(channel, kind, text=None, file_path=None) -> (decision: str, violations: list)`
     - `kind=="text"` → `engine.analyze(text, channel)`
     - `kind=="file"` → use `extractor.is_tabular(file_path)` (at `analyzer/extractor.py:54`). If tabular → `extract_tabular` + `engine.analyze_tabular`; else → `extract_text` + `engine.analyze`.
     - Map `AnalysisResult.applied_action` to wire string: `block → BLOCK`, `allow|allow_log → ALLOW`.
     - On `allow_log` or `block`, log the full violations list (use `AnalysisResult.violations` from `analyzer/engine.py:60`).
   - For Phase 1 the engine is built once at startup, no reload yet.
4. `orchestrator/server.py`:
   - Single-instance pipe server (pywin32 `CreateNamedPipe` with `nMaxInstances=1`), same pattern as current `stub_consumer.py:run`.
   - Read JSON, dispatch to `policy_manager.analyze`, write response, close.
   - No thread pool yet — everything in one thread.
5. `orchestrator/__main__.py` — parses `--foreground` / `--install` / `--uninstall` / `--service`. Phase 1 implements only `--foreground`.
6. `interceptors/browser/addon.py` — change the payload sent to the pipe in `_consult_policy` (currently at `addon.py:617`) to the new schema:
   ```python
   payload = {
       "channel": "browser",
       "kind": "file",
       "file_path": temp_path,
       "metadata": {
           "url": ..., "filename": ..., "size_bytes": ..., ...
       },
   }
   ```
   Orchestrator owns temp-file cleanup; remove the addon-side `_delete_temp_file` call on success (keep it on failure / consumer-not-received, matching existing semantics at `addon.py:158`).
7. .NET: new `src/AgentCore/PipeAgentCore.cs` implementing `IAgentCore` (at `src/AgentCore/IAgentCore.cs:3`) using `System.IO.Pipes.NamedPipeClientStream`. Serialize request with `System.Text.Json`. Propagate `CancellationToken` to the stream's async operations so the existing `_currentCts` supersession still works. Cap text at 1 MB before sending. Delete `src/AgentCore/StubAgentCore.cs`. Update `src/ClipboardInterceptor/Program.cs:14` to construct `PipeAgentCore` with pipe name from a small local JSON file (or compiled-in default for now; real config plumbing can wait).
8. Orchestrator graceful shutdown: Ctrl-C in foreground mode closes pending pipe, flushes logs.

Verify (three shells):
1. `python -m orchestrator --foreground`
2. `cd interceptors/browser && mitmdump -s addon.py --listen-port 8080` — set browser proxy manually to `127.0.0.1:8080` for this test.
3. `dotnet run --project src/ClipboardInterceptor`
- Copy `"4111111111111111 credit card"` → clipboard replaced with `[DLP: Content Blocked]`, orchestrator logs violation for `block_visa_browser` (policy needs a `clipboard` channel added for this test).
- Upload a PDF through Drive → addon sends temp path → orchestrator extracts + analyzes → `ALLOW`/`BLOCK` returned.
- Kill orchestrator mid-analysis → clipboard interceptor fails closed per config.

### Phase 2 — Concurrency, hot-reload, clipboard supersession, robustness

Goal: real load works; policies reload live; superseded clipboard decisions drop cleanly.

**Design note — pipe I/O ownership:** An earlier design passed the raw pipe `HANDLE` from the accept thread to a `ThreadPoolExecutor` worker and had the worker write the response. This caused a silent hang: if the worker encountered any issue before writing, the client's `ReadFile` blocked forever with no log output. The implemented design keeps pipe I/O entirely in the accept thread; the dispatcher provides a synchronous `analyze()` call that the accept thread blocks on, then the accept thread writes the response itself.

Steps:
1. `orchestrator/server.py` multi-instance accept:
   - `nMaxInstances=PIPE_UNLIMITED_INSTANCES` (avoids the race where all `pipe_listeners` slots are full when an accept thread loops back to create a replacement handle).
   - `run()` spawns `pipe_listeners` daemon threads, each running `_accept_loop()`.
   - `_accept_loop()` per iteration: `CreateNamedPipe` → `ConnectNamedPipe` → `_handle_connection(handle)`.
   - `_handle_connection(handle)`: `ReadFile` → `json.loads` → `dispatcher.analyze(request)` (blocks) → `WriteFile` → `FlushFileBuffers` → `DisconnectNamedPipe` → `CloseHandle`. All pipe I/O stays in the accept thread.
   - `stop()` sends `pipe_listeners` throwaway connections to unblock all blocked `ConnectNamedPipe` calls.
   - `ERROR_PIPE_CONNECTED` (535) from `ConnectNamedPipe` is treated as a success (client connected before `ConnectNamedPipe` was called).
2. `orchestrator/dispatcher.py`:
   - Two `concurrent.futures.ThreadPoolExecutor`s: `clipboard_pool` (size `clipboard_workers`) and `browser_pool` (size `browser_workers`).
   - Public method: `analyze(request) -> (decision: str, write_response: bool)`. The accept thread calls this synchronously and blocks until analysis completes.
   - Routes by `channel`: clipboard → `_analyze_clipboard`, everything else → `_analyze_browser`.
   - Both paths use `future.result(timeout=4.0)` — if analysis hangs for any reason, "BLOCK" is returned within 4 s (inside the client's 5 s pipe timeout).
   - Clipboard supersession state:
     ```python
     _clip_seq: int = 0
     _clip_lock: threading.Lock
     _clip_inflight: dict[int, threading.Event]  # seq → cancel flag
     ```
     On new clipboard request: acquire lock, increment seq, set cancel flag on all lower in-flight seqs, record own seq. After `future.result()` returns, check `cancel_flag.is_set()` — if set, return `write_response=False` and log `superseded`. The accept thread skips `WriteFile` and closes the pipe silently.
3. `orchestrator/policy_manager.py` hot-reload:
   - `watchdog.observers.Observer` on the directory containing `analyzer/policies.yaml`.
   - `_ReloadHandler.on_modified`: debounce 500 ms via `threading.Timer`, then call `_reload_engine()`.
   - `_reload_engine()`: construct `DLPEngine(policies_file)`, atomically assign `self._engine`. Any `analyze()` call already in progress holds its own `engine` snapshot and is unaffected. Failed reload keeps old engine.
   - `stop()` shuts down the observer.
   - `analyze()` snapshots `engine = self._engine` at entry so the engine reference is stable for the full call even if a reload fires mid-analysis.
4. Clipboard `PipeAgentCore` (already done in Phase 1): fail-closed on any `Exception`.
5. Browser addon `fail_behavior: block` (already done in Phase 1).

Verify:
- Script 10 concurrent browser uploads + 20 rapid clipboard pastes; confirm no orchestrator deadlock, no clipboard request waiting behind browser work (measure with timestamps in log).
- While orchestrator is running, edit `analyzer/policies.yaml` (add a new denylist keyword) — save; within ~1 s, a new copy containing that keyword is blocked.
- Paste A, then within 100 ms paste B — orchestrator log shows A's analysis marked `superseded`, clipboard reflects B's decision only.

### Phase 3 — Install / uninstall: cert, proxy, service registration

Goal: one command stands up the whole endpoint; one tears it down cleanly. Interceptors still manually launched.

Steps:
1. `orchestrator/installer.py`:
   - `--install`:
     a. If `~/.mitmproxy/mitmproxy-ca-cert.cer` is missing, run `mitmdump` briefly in a scratch dir to generate the CA (exit after ~2 s).
     b. Import the CA into the `Root` store via `certutil -addstore -f Root <path>` (subprocess). Record thumbprint to `%PROGRAMDATA%\DLP\state\installed_ca.txt` for uninstall.
     c. Snapshot HKCU proxy keys (`ProxyEnable`, `ProxyServer`, `ProxyOverride`) under `%PROGRAMDATA%\DLP\state\proxy_backup.json`, then set `ProxyEnable=1`, `ProxyServer=127.0.0.1:<port>`, `ProxyOverride=<bypass>`. (Phase 3: installing admin's HKCU only. Phase 4 will extend this to any logged-on user.)
     d. Register Windows service: `sc create DLPAgent binPath= "<python> -m orchestrator --service" start= auto` (or the pywin32 equivalent via `win32serviceutil.HandleCommandLine`).
     e. `sc start DLPAgent`.
   - `--uninstall`: mirror, idempotent. Stop service, unregister, `certutil -delstore Root <thumbprint>`, restore snapshotted proxy keys.
2. `orchestrator/service.py`:
   - `class DLPAgentService(win32serviceutil.ServiceFramework)` with `SvcDoRun` → constructs and runs the same orchestrator entrypoint used by `--foreground`, minus console handler.
   - `SvcStop` → graceful drain: stop accepting new pipe connections, wait up to 5 s for in-flight requests, kill children, exit.
3. Child-process launching still uses the current user's session for now (foreground-style spawning with `subprocess.Popen`) — session-aware spawning is Phase 4.

Verify:
- `python -m orchestrator --install` — `certmgr.msc` shows mitmproxy CA under Trusted Root, `reg query "HKCU\...\Internet Settings"` shows proxy set, `sc query DLPAgent` shows RUNNING.
- Launch mitmdump + clipboard interceptor manually, exercise both channels — confirm still end-to-end.
- `python -m orchestrator --uninstall` — CA gone, proxy restored to snapshot, service removed.

### Phase 4 — LocalSystem service with user-session child spawning

Goal: orchestrator runs as `LocalSystem`, spawns interceptors into the active user session, handles logon/logoff, restarts crashed children.

Steps:
1. `orchestrator/session.py`:
   - `SvcOtherEx` handles `SERVICE_CONTROL_SESSIONCHANGE`.
   - On `WTS_SESSION_LOGON`:
     - `win32ts.WTSQueryUserToken(session_id)` → user token.
     - `win32security.DuplicateTokenEx(..., TokenPrimary)`.
     - `win32profile.CreateEnvironmentBlock(user_token, False)`.
     - `win32process.CreateProcessAsUser(user_token, ..., STARTF_USESHOWWINDOW | CREATE_UNICODE_ENVIRONMENT, env_block, ...)` for:
       - `mitmdump.exe -s <addon_script> --listen-port <port>`
       - `ClipboardInterceptor.exe`
     - Write that user's HKCU proxy keys (load `HKU\<sid>\...` via `RegLoadKey`/`RegOpenKeyEx`).
   - On `WTS_SESSION_LOGOFF`:
     - Terminate child handles for that session.
     - Restore HKCU proxy keys from the backup saved on logon.
2. `orchestrator/supervisor.py`:
   - Owns `(session_id, name) -> process_handle` table.
   - Waits on child handles in a dedicated thread (`WaitForMultipleObjects`).
   - On unexpected exit: check restart counter for that (session, name) in the last `restart_window_seconds`. If under `max_restarts`, respawn. Else, log `giving up` and surface via `dlp-ctl status`.
   - Reset counter after `stable_uptime_reset_seconds` of stable running.
3. Named-pipe security: build a `SECURITY_DESCRIPTOR` granting `FILE_GENERIC_READ | FILE_GENERIC_WRITE` to `Authenticated Users` SID. Orchestrator runs as LocalSystem but the pipe must be openable by the user-session interceptors.
4. Interceptor binaries location: reference config `paths.clipboard_exe`; for `--install`, copy the published `ClipboardInterceptor.exe` to `%ProgramFiles%\DLP\` and use that path. For dev (`--foreground`), use the `bin/Debug/...` path from config.

Verify:
- Install service, reboot, log in → verify (Task Manager) mitmdump and ClipboardInterceptor processes exist under your session.
- Kill `mitmdump.exe` from Task Manager → orchestrator log shows respawn; upload via browser again still intercepted.
- Log out → processes gone; log back in → processes back.
- Stop service while interceptors are running (`sc stop DLPAgent`) → clean shutdown, children gone, proxy restored.

### Phase 5 — Admin control, drain, polish

Goal: operator ergonomics and hardening niceties.

Steps:
1. `orchestrator/ctl.py` — `dlp-ctl` CLI:
   - `dlp-ctl status` — prints uptime, in-flight counts per channel, last policy reload timestamp, child process states.
   - `dlp-ctl reload` — send `{"cmd":"reload"}` on the control pipe; forces an immediate reload without waiting for the file-watch debounce.
   - `dlp-ctl tail` — stream last N log lines.
2. Control-pipe ACL: only `BUILTIN\Administrators` allowed.
3. Structured JSON event log alongside the human log (`%PROGRAMDATA%\DLP\logs\events.jsonl`). One line per decision: `{ts, channel, kind, filename?, url?, decision, violation_ids, elapsed_ms}`. Future SIEM-ready.
4. Graceful drain on `SvcStop`:
   - Close listening pipe instances (no new accepts).
   - Wait on per-pool executor futures up to 5 s (configurable).
   - Terminate children, restore proxy, exit.
5. README: install / test / uninstall flow for the testing device.

Verify:
- `dlp-ctl status` from an elevated prompt returns data; from a standard prompt returns access-denied.
- `dlp-ctl reload` after editing policies.yaml reloads immediately (log line within 100 ms).
- `sc stop DLPAgent` during a burst of 20 in-flight requests completes in under ~5 s with zero partial responses.

---

## Critical files to modify / create (quick reference)

Modify:
- `addon.py` → moved to `interceptors/browser/addon.py`; change `_consult_policy` payload shape (currently `addon.py:143-151`).
- `pipe_client.py` → moved to `interceptors/browser/pipe_client.py`; protocol unchanged.
- `config.py`, `config.yaml` → moved to `interceptors/browser/`.
- `src/ClipboardInterceptor/Program.cs:14` — swap `new StubAgentCore()` for `new PipeAgentCore(pipeName, config)`.

Create:
- All files under `orchestrator/` (listed above).
- `src/AgentCore/PipeAgentCore.cs` implementing `IAgentCore` from `src/AgentCore/IAgentCore.cs:3`.
- `orchestrator.yaml` at repo root.

Delete:
- `stub_consumer.py`, `src/AgentCore/StubAgentCore.cs`, `src/QueueManager/`.

Reuse (do not reimplement):
- `DLPEngine` at `analyzer/engine.py:87` — `analyze` and `analyze_tabular`.
- `extract_text` / `extract_tabular` / `is_tabular` at `analyzer/extractor.py`.
- `load_policies` at `analyzer/policy.py:28`.
- `ClipboardMonitor` at `src/ClipboardInterceptor/ClipboardMonitor.cs` and the supersession logic in `ClipboardInterceptorService.cs` — both stay as-is; only the injected `IAgentCore` implementation changes.
- `pipe_client.send_and_receive` pattern from `pipe_client.py:17` — `PipeAgentCore.cs` on the .NET side mirrors the same one-request-per-connection flow.

---

## End-to-end verification (final)

Run after Phase 5:

1. Fresh VM / restore snapshot. `python -m orchestrator --install`.
2. Reboot. Log in as the admin test user.
3. Confirm (Task Manager / `sc query DLPAgent` / `certmgr.msc` / registry):
   - Service `DLPAgent` running.
   - Child `mitmdump.exe` and `ClipboardInterceptor.exe` running in your session.
   - mitmproxy CA in Trusted Root.
   - HKCU proxy pointing at `127.0.0.1:8080`.
4. Copy `"4111111111111111 credit card"` → clipboard replaced with block notification; `events.jsonl` gained a block entry.
5. Upload a PDF containing a Visa number to Google Drive through Chrome → upload fails with 403; `events.jsonl` gained a block entry for the browser channel.
6. Kill `mitmdump.exe` → respawn within a second; repeat step 5, still blocked.
7. Edit `analyzer/policies.yaml` — add a new denylist keyword `"radioactive"`; within ~1 s copy text containing `"radioactive"` → blocked.
8. `dlp-ctl status` returns expected metrics.
9. `python -m orchestrator --uninstall` → service gone, CA gone, proxy restored; reboot; confirm browser works normally and clipboard is unrestricted.

---

## Risks surfaced

1. **`CreateProcessAsUser` / Session 0 isolation** — most likely source of Phase-4 pain. `WTSQueryUserToken` + `DuplicateTokenEx(TokenPrimary)` + `CreateEnvironmentBlock` all need to be right; one missing flag and the child silently runs in Session 0 with no UI. Fallback plan: if this proves too flaky, make interceptors per-user Task Scheduler tasks triggered at logon; the orchestrator just hosts the pipe. This keeps Phases 0-3 valid.
2. **mitmproxy CA bootstrap** — `~/.mitmproxy/` doesn't exist until mitmdump runs once. Installer runs it briefly in a scratch dir to generate, then copies out.
3. **HKCU per session** — a service running as `LocalSystem` cannot just write `HKCU` — it's the service's own profile. Phase 4 uses `RegLoadKey`/`HKU\<SID>` or writes on behalf of the user-session child process at spawn time.
4. **Big browser file extraction blocking a worker** — 50 MB PDF extraction can take several seconds; 3 concurrent ones fills the browser pool. Clipboard is protected by its own pool, but browser itself can backlog. Pool size is the escape hatch; users can raise `browser_workers` if needed.
5. **Wasted CPU on superseded clipboard analyses** — we accepted this trade (coarse-grained cancel). Worst case: user pastes 10 different 1 MB texts in 2 seconds → 10 analyses run to completion, 9 decisions dropped. Bounded and acceptable.
