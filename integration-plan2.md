# DLP Endpoint Agent — Re-planned Phased Integration

## Context

The original integration plan at `D:\Code\GithubPublishEndpointDLP\integration-plan.md` was written when the project had three components in isolation: an analyzer, a browser interceptor, and a clipboard interceptor. Since then:

1. **Phases 0–2 of the original plan are largely implemented in code** but only partially verified end-to-end. The user has identified four specific gaps that need validation: multi-instance pipe concurrency, policy hot-reload under load, dispatcher fail-closed timeout, and clipboard supersession edge cases.
2. **A fourth interceptor — peripheral_storage — has been added** with four sub-components (Controller in C#, Payload C++ DLL, ShellExtension C++ COM, TransferAgent C# WinForms). The original plan explicitly carved this channel out as "stays a stub". It is no longer a stub.
3. **The orchestrator's dispatcher already routes a `peripheral_storage` channel** (`orchestrator/dispatcher.py:51,86–106`) and TransferAgent already speaks the right wire protocol (`interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs`). The IPC bridge is in place; Phase B unified all non-policy configuration into a single `config.yaml` at the repo root (renamed from `orchestrator.yaml`) that every component reads via `DlpShared.ConfigLocator`. Nothing supervises Controller yet (Phase C) and no unified install exists yet (Phase D).
4. **Phases 3–5 of the original plan (installer, LocalSystem service, session-aware spawning, admin control) are still 1-line stubs** in `orchestrator/`.

This re-plan supersedes the original plan from Phase 2 onward. It is structured as a high-level phase list; **each phase will be planned in detail in a separate follow-up session**, so this file intentionally stays brief on per-step work and explicitly omits test/verification steps (per user instruction — they would be inaccurate at this resolution).

## Locked decisions (from this session)

| # | Decision |
|---|---|
| 1 | Orchestrator supervises Controller.exe (alongside mitmdump and ClipboardInterceptor.exe). |
| 2 | All four Phase-2 gaps (multi-instance, hot-reload, timeout, supersession) must be validated and fixed before adding new functionality. |
| 3 | Scope: full re-plan from current state forward. Original Phase 3/4/5 are reorganized to absorb peripheral_storage. |
| 4 | Configuration: single `config.yaml` (renamed from `orchestrator.yaml` during Phase B implementation) with named sections. Each section is clearly labelled so it is obvious which component a setting belongs to. **`analyzer/policies.yaml` stays separate** (policy ≠ config). |
| 5 | Orchestrator installer handles ShellExtension registration; the current `interceptors/peripheral_storage/verify-install.ps1` is replaced by the orchestrator's installer flow. |
| 6 | Process context for Controller and TransferAgent under a LocalSystem service is an **open question to investigate in Phase E**, not a pre-committed design. |

## Current state snapshot

**Implemented and roughly working** (Phase 0–2 of old plan, Phase A, **and Phase B**):
- `orchestrator/server.py` — multi-instance pipe server, accepts JSON, dispatches, writes response in accept thread.
- `orchestrator/dispatcher.py` — three per-channel `ThreadPoolExecutor`s (clipboard/browser/peripheral), 4 s timeout fail-closed, clipboard supersession via `_clip_seq` / `_clip_inflight`.
- `orchestrator/policy_manager.py` — `DLPEngine` wrapper, `watchdog` hot-reload with 100 ms debounce + on_modified/on_moved/on_created handlers, lock-guarded snapshot-on-entry (strict bar).
- `orchestrator/config.py` — dataclass loader for the central `config.yaml`; carries the raw parsed tree on `OrchestratorConfig.raw` for the ctl-pipe broadcaster.
- `orchestrator/logging_setup.py` — rotating file + console.
- `orchestrator/__main__.py` — `--foreground` wires PipeServer + CtlServer + ConfigWatcher; selective-skip of non-hot-reloadable `data_pipe` / `ctl_pipe` lives here. Other subcommands print "not implemented".
- `orchestrator/ctl_server.py` — **NEW (Phase B):** single-instance ctl-pipe server, rejects duplicate subscribes with `already_subscribed`, projects per-component sections from raw config, push delivery with 500 ms write deadline.
- `orchestrator/config_watcher.py` — **NEW (Phase B):** watchdog-based FileSystemWatcher on `config.yaml`, 200 ms debounce, parses + invokes on_change callback.
- `interceptors/browser/addon.py` + `pipe_client.py` + `config.py` + `ctl_pipe_subscriber.py` — addon reads central `config.yaml` (via `DLP_CONFIG_PATH` env var → walk-up + sentinel), subscribes to ctl-pipe for live config updates.
- `src/AgentCore/PipeAgentCore.cs` — real pipe client; supports a `Func<(string, int)>` provider for hot-reloadable timeout; fail-closed on any exception.
- `src/ClipboardInterceptor/ClipboardHistoryEnforcer.cs` — keeps Windows clipboard history disabled via `RegNotifyChangeKeyValue`.
- `src/ClipboardInterceptor/Program.cs` + `ClipboardConfigHolder.cs` — reads central `config.yaml` via `DlpShared.ConfigLocator`, subscribes to ctl-pipe.
- `src/DlpShared/` — **NEW (Phase B):** shared C# library with `ConfigLocator` (env var → walk-up N=8 + sentinel check) and `CtlPipeSubscriber` (long-lived message-mode subscriber, exponential-backoff reconnect, handles `already_subscribed` retryably).
- `analyzer/cli_extractor.py` — standalone CLI for file-text extraction.

**Peripheral_storage components (Phase B integrated):**
- `interceptors/peripheral_storage/Controller/Program.cs` + `Config/AppConfig.cs` — Controller now reads the central `config.yaml`'s `peripheral_storage` section via `DlpShared.ConfigLocator`. The legacy FileSystemWatcher is replaced by a `CtlPipeSubscriber`; the existing selective-update logic in `TryReload(AppConfig)` (target_processes / fail_mode / payload_dll_path / `shared_memory_name` rejection) is preserved. `running-config.yaml` is retained as an audit-trail artifact.
- `interceptors/peripheral_storage/Payload/{dllmain,hook}.cpp` — injected DLL that hooks `NtCreateFile`; reads removable-drive seqlock from `Global\UsbDlpDriveMap`; deactivates on `AliveMutex` release.
- `interceptors/peripheral_storage/ShellExtension/DlpContextMenu.cpp` — COM context-menu handler ("Transfer to USB (DLP Protected)") that reads `HKCU\Software\DLPAgent\TransferAgentPath` and launches TransferAgent with `--dest <drive> <files...>`.
- `interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs` + `Program.cs` — TransferAgent now reads central `config.yaml` at startup via `DlpShared.ConfigLocator` (one-shot disk read; intentionally does NOT subscribe to ctl-pipe given its per-file lifecycle). Sends the same `{channel:"peripheral_storage", kind:"file", ...}` payload as before.
- `interceptors/peripheral_storage/verify-install.ps1` — current install path: HKCU CLSID + context-menu handler + agent-path registration. **No-admin install** — useful as a reference but slated to be replaced by the orchestrator's installer in Phase D.

**Stubs (1-line docstrings only):**
- `orchestrator/supervisor.py`, `session.py`, `service.py`, `installer.py`.

## Critical files referenced throughout this plan

- `orchestrator/server.py`, `orchestrator/dispatcher.py`, `orchestrator/policy_manager.py`, `orchestrator/config.py`
- `orchestrator/ctl_server.py`, `orchestrator/config_watcher.py` (Phase B)
- `orchestrator/supervisor.py` (to-build), `orchestrator/installer.py` (to-build), `orchestrator/service.py` (to-build), `orchestrator/session.py` (to-build)
- `src/DlpShared/ConfigLocator.cs`, `src/DlpShared/CtlPipeSubscriber.cs` (Phase B; referenced by AgentCore, Controller, TransferAgent)
- `interceptors/peripheral_storage/Controller/Program.cs`, `Controller/Config/AppConfig.cs` (reads `peripheral_storage` section of the central `config.yaml`)
- `interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs`, `TransferAgent/Program.cs`
- `interceptors/peripheral_storage/ShellExtension/DlpContextMenu.cpp`, `ShellExtension/DlpContextMenu.h` (CLSID lives here)
- `interceptors/peripheral_storage/verify-install.ps1`
- `interceptors/browser/config.py`, `interceptors/browser/ctl_pipe_subscriber.py` (Phase B; addon reads central `config.yaml`)
- `config.yaml` (central, at repo root; renamed from `orchestrator.yaml` in Phase B), `analyzer/policies.yaml`

---

## Phases

### Phase A — Stabilize Phase-2 work (validation + bug-fix)

**Goal:** prove the four flagged behaviors actually hold under stress, and fix anything that does not. No new functionality.

**Scope of follow-up planning session for this phase:**
- Define repeatable load harnesses (Python script driving N concurrent pipe clients; shell loop banging clipboard) for each of the four gaps.
- Identify likely failure modes per gap (e.g. for hot-reload under load: in-flight call that reads `self._engine` mid-swap; for supersession: race between cancel-flag set and `cancel_flag.is_set()` check at `dispatcher.py:141`).
- Decide whether to add structured request IDs / instrumentation (already partially present via `req_id`) for debugging.
- Confirm `_ANALYSIS_TIMEOUT = 4.0` (`dispatcher.py:15`) is well under the client-side 5 s pipe timeout in both `interceptors/browser/pipe_client.py` and `src/AgentCore/PipeAgentCore.cs`.

**Done when:** all four gaps reproduce reliably under the harness and the orchestrator returns correct decisions / drops superseded responses without partial writes or deadlocks.

### Phase B — Unify configuration into a single sectioned `config.yaml` ✅ COMPLETED

**Goal (achieved):** all non-policy configuration lives in one sectioned `config.yaml` at the repo root; each component reads only its labelled section. `analyzer/policies.yaml` stays separate (policy ≠ config).

**Outcomes (locked decisions from the follow-up Phase B planning session):**
- Central file is **`config.yaml`** (renamed from `orchestrator.yaml`). Walk-up discovery (`DLP_CONFIG_PATH` env var → walk up N=8 from the executable) **requires the candidate file to contain a top-level `data_pipe:` key (the sentinel)** so a stray unrelated `config.yaml` cannot shadow it.
- **Controller, ClipboardInterceptor, browser addon, TransferAgent all read `config.yaml` directly** at startup. No installer-synced shadow files. The legacy `interceptors/browser/config.yaml` and `interceptors/peripheral_storage/Controller/Config/config.yaml` are deleted.
- **Hot-reload is centralised** via a new ctl-pipe push protocol (`\\.\pipe\dlp_agent_ctl`). Orchestrator owns a `FileSystemWatcher` on `config.yaml` and pushes per-component section updates to each subscribed client. Controller, ClipboardInterceptor, and the browser addon subscribe; TransferAgent does NOT subscribe (per-file lifecycle, one-shot read).
- **`data_pipe` and `ctl_pipe` are non-hot-reloadable at the field level**: if a yaml save touches them, the orchestrator logs `"<field> change requires restart; keeping <old>"`, overrides them back to the in-use values in the broadcast payload, and **still propagates the other fields** in the same save (same pattern as Controller's existing `shared_memory_name` rejection).
- **Subscriber registry is `dict[str, Handle]` (single-instance per component).** Duplicate subscribes get `{"type":"error","code":"already_subscribed"}` and connection-close, which loudly catches duplicate-launch dev mistakes. Reconnect-after-pipe-break self-resolves via worker-thread EOF cleanup.
- Shared C# code lives in `src/DlpShared/` (a new project). `AgentCore`, `Controller`, and `TransferAgent` all `<ProjectReference>` it. `PipeAgentCore` gained a `Func<(string, int)>` provider constructor overload so hot-reloadable timeouts take effect on the next `AnalyseAsync` without re-instantiating.
- Schema sketched in the old plan got slimmed: `paths.controller_exe / transfer_agent_exe / shell_extension_dll / payload_dll` are NOT in `config.yaml` yet — they'll be added in Phase C (supervisor needs them) and Phase D (installer needs them). The `peripheral_storage:` section has the Controller fields at its top level and a nested `transfer_agent:` subsection with the two TransferAgent timeouts.

**Verification (all green):** `scripts/harness/test_ctl_pipe.py` exercises the snapshot path and a selective-skip end-to-end (one non-hot-reloadable + two hot-reloadable fields in the same save); the five `src/AgentCore.Tests/ConfigLocatorTests.cs` cases cover env-var-valid, env-var-fails-sentinel, walk-up-finds, walk-up-skips-misleading, and none-found-throws. Original 7 Phase A pytests + 2 Phase A AgentCore tests continue to pass unchanged.

### Phase C — Supervisor: spawn, watch, restart all four child processes (foreground mode)

**Goal:** `python -m orchestrator --foreground` starts the orchestrator **and** launches/supervises mitmdump, ClipboardInterceptor.exe, and Controller.exe. Crashes restart per policy (max 3 in 60 s window). Ctrl-C kills children cleanly. TransferAgent is NOT supervised — it is launched on demand by ShellExtension and must never be hooked (per memory).

**Scope of follow-up planning session:**
- API for `orchestrator/supervisor.py`: a `Supervisor` class taking a list of `ChildSpec(name, exe, args, working_dir, restart_policy)` and exposing `start_all()` / `stop_all()` / status snapshot. Wait on child handles via a dedicated thread (`WaitForMultipleObjects` per old Phase 4 step 2 — applicable here in foreground form too).
- How `--foreground` wires Supervisor between PolicyManager and PipeServer in `orchestrator/__main__.py:_run_foreground`.
- Restart counter reset after `stable_uptime_reset_seconds` of stable running (`config.yaml` `supervisor:` section).
- Logging conventions per child (separate log file per child? interleaved in `dlp-agent.log` with prefix?).
- For Controller specifically: it currently relies on its own Ctrl+C → `cts.Cancel()` path (`Controller/Program.cs:34–39`) to release the alive mutex *before* `ProcessMonitor.Dispose()` blocks. Supervisor must use a clean shutdown signal (CTRL_BREAK_EVENT to the console process group, or a named event the Controller waits on) — NOT `TerminateProcess`, which would leave hooks active in injected processes. Confirm signal mechanism in the follow-up session.
- Whether mitmdump needs the working directory set to `interceptors/browser/` (matches old plan Phase 0 note).

**Done when:** killing any of the three managed children from Task Manager produces a respawn in logs; Ctrl-C on the orchestrator kills all three cleanly with no orphaned hooks.

### Phase D — Installer / Uninstaller (replaces `verify-install.ps1` and standalone setup)

**Goal:** one `python -m orchestrator --install` stands up the full endpoint; one `--uninstall` reverses everything idempotently.

**Scope of follow-up planning session:**
- Layout under `%ProgramFiles%\DLP\`: where the orchestrator, mitmproxy CA, Controller.exe, Payload.dll, TransferAgent.exe, DlpShellExt.dll, and `config.yaml` get copied.
- Build orchestration: should `--install` build the C# / C++ projects (like `verify-install.ps1` does today via `dotnet publish` + `msbuild`), or expect pre-built artifacts in a known location? Recommendation: expect pre-built; emit a clear error if missing. Building is a developer concern.
- mitmproxy CA bootstrap (old plan Phase 3 step 1a–b) — run `mitmdump` briefly to generate `~/.mitmproxy/`, then `certutil -addstore -f Root <cer>`. Record thumbprint in `%PROGRAMDATA%\DLP\state\installed_ca.txt`.
- HKCU proxy backup + set (old plan Phase 3 step 1c). Phase 3 only handles installer's HKCU; Phase E extends to other sessions.
- Windows service registration via `win32serviceutil.HandleCommandLine` (old plan Phase 3 step 1d) — pairs with Phase E `service.py`.
- ShellExtension registration: today's `verify-install.ps1` writes HKCU keys (CLSID, `*\shellex\ContextMenuHandlers\DLPTransfer`, `Directory\shellex\ContextMenuHandlers\DLPTransfer`, `DLPAgent\TransferAgentPath`). Decide in follow-up: keep HKCU (per-user, no admin) vs. promote to HKLM (machine-wide, requires admin). HKLM matches a LocalSystem-service install model better. The CLSID `{B3A1C2D4-E5F6-7890-ABCD-EF1234567890}` is defined in `ShellExtension/DlpContextMenu.h` — installer must read it from there or be kept in sync.
- Uninstall idempotency: each step must succeed if its target is already absent (no `if exists` errors).

**Done when:** install on a fresh VM produces a working endpoint without manual steps; uninstall returns the VM to a clean state confirmed by `certmgr.msc`, `reg query`, `sc query DLPAgent`, and absence of context-menu entry.

### Phase E — LocalSystem service + Session-aware spawning + Process context resolution

**Goal:** orchestrator runs as a `LocalSystem` Windows service, spawns interceptors into the active user session(s), handles logon/logoff, restarts crashed children. **This is the phase where the open process-context question gets answered.**

**Scope of follow-up planning session:**
- **Open investigation (top priority):** does DLL injection from a LocalSystem-context Controller into a user-session `explorer.exe` actually work on Windows 11 24H2? Investigate `SeDebugPrivilege` + cross-session handle access. Options:
  - **(a)** Controller runs as LocalSystem (same context as the service) and uses cross-session injection. Simpler supervision but riskier on Win11.
  - **(b)** Controller is launched via `CreateProcessAsUser` into the user session, elevated. Matches the rest of the user-session children but requires UAC/elevation in user session.
  - **(c)** Fallback (per old plan Phase 4 risk #1): per-user Task Scheduler tasks at logon, with the orchestrator service just hosting the pipe.
- TransferAgent stays in user session (it's a WinForms UI); ShellExtension naturally runs in user-session explorer.exe — both unchanged by this phase.
- `orchestrator/service.py` — `win32serviceutil.ServiceFramework` subclass; `SvcDoRun` invokes the same entrypoint as `--foreground` minus console.
- `orchestrator/session.py` — `SvcOtherEx` handles `SERVICE_CONTROL_SESSIONCHANGE`; on `WTS_SESSION_LOGON`: `WTSQueryUserToken` → `DuplicateTokenEx(TokenPrimary)` → `CreateEnvironmentBlock` → `CreateProcessAsUser` for each user-session child. On `WTS_SESSION_LOGOFF`: terminate that session's children, restore proxy backup.
- Per-session HKCU proxy keys via `RegLoadKey` / `HKU\<SID>` (old plan Phase 4 step 1, Risk #3).
- Named-pipe security descriptor: `Authenticated Users` granted `FILE_GENERIC_READ | FILE_GENERIC_WRITE` on data pipe (old plan Phase 4 step 3) — required so user-session interceptors can open the pipe.
- Supervisor extension: existing foreground supervisor (Phase C) is extended with a session-aware spawn helper. Per-session child table keyed by `(session_id, child_name)`.

**Done when:** install + reboot + logon results in all four interceptor processes (mitmdump, ClipboardInterceptor, Controller, and ShellExtension-launched TransferAgent on demand) running in the correct contexts; logoff cleans them up; `sc stop DLPAgent` is a clean drain.

### Phase F — Admin control, drain, polish

**Goal:** operator ergonomics + hardening that doesn't fit cleanly into earlier phases.

**Scope of follow-up planning session:**
- `orchestrator/ctl.py` is already implemented in some form — assess current state in the follow-up session before re-designing. Commands: `dlp-ctl status` (uptime, in-flight counts per channel, last reload ts, child states from Supervisor), `dlp-ctl reload`, `dlp-ctl tail`.
- Control-pipe ACL (`\\.\pipe\dlp_agent_ctl`) restricted to `BUILTIN\Administrators` (old plan Phase 5 step 2).
- Structured JSON event log `%PROGRAMDATA%\DLP\logs\events.jsonl` — one line per decision, includes channel, kind, filename/url, decision, violation IDs, elapsed ms (old plan Phase 5 step 3).
- Graceful drain on `SvcStop`: close listening pipes, wait up to `drain_timeout_seconds` on in-flight futures per pool, then terminate children, restore proxy, exit (old plan Phase 5 step 4).

**Done when:** the system is operationally observable from an admin shell, decisions are auditable from `events.jsonl`, and service stop is always clean.

---

## Open questions tracked for per-phase sessions

These are intentionally not resolved here — they will be answered when each phase is planned in detail:

1. ~~**Phase B:** Controller reads `orchestrator.yaml` directly, or installer-synced `config.yaml`?~~ **RESOLVED:** Controller reads central `config.yaml` directly via `DlpShared.ConfigLocator`. No shadow file.
2. ~~**Phase B:** Does Controller's own `FileSystemWatcher` hot-reload stay, or is hot-reload centralised in the orchestrator?~~ **RESOLVED:** Centralised. Controller subscribes to the orchestrator's ctl-pipe and re-applies its existing selective-update logic on each push. The FileSystemWatcher on the legacy local file is removed.
3. **Phase C:** Shutdown signal mechanism for Controller (CTRL_BREAK, named event, or other)?
4. **Phase C:** Per-child log streams vs. interleaved logging?
5. **Phase D:** Build during install vs. expect pre-built?
6. **Phase D:** ShellExtension registration in HKCU (current, no-admin) vs HKLM (machine-wide)?
7. **Phase E (the big one):** Controller process context — LocalSystem, user-session-via-CreateProcessAsUser, or Task Scheduler fallback?
8. **Phase E:** Multi-session support — single active user session only, or all logged-on sessions?
