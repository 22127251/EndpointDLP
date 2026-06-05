# DLP Endpoint Agent — Re-planned Phased Integration

## Context

The original integration plan at `D:\Code\GithubPublishEndpointDLP\integration-plan.md` was written when the project had three components in isolation: an analyzer, a browser interceptor, and a clipboard interceptor. Since then:

1. **Phases 0–2 of the original plan are implemented and validated.** Phase A added 7 pytests + 2 AgentCore tests covering the four flagged gaps (multi-instance pipe concurrency, policy hot-reload under load, dispatcher fail-closed timeout, clipboard supersession). All pass.
2. **The peripheral_storage interceptor was added** (Controller C# + Payload C++ DLL + ShellExtension C++ COM + TransferAgent C# WinForms) and is now fully integrated end-to-end through Phase D.
3. **Phases A → E are complete.** Phase B unified config into a single sectioned `config.yaml` with ctl-pipe hot-reload. Phase C added the foreground Supervisor for mitmdump / ClipboardInterceptor / Controller (with restart watcher, per-child rotating logs, `CTRL_BREAK_EVENT` shutdown). Phase D shipped the `python -m orchestrator --install` / `--uninstall` flow that stands up a `%ProgramFiles%\DLP\` installation with a bundled Python 3.13 embed, HKLM ShellExt registration, mitmproxy CA bootstrap (via `CertStore.from_store` API), HKCU proxy redirect, and a registered `DLPAgent` Windows service (placeholder `SvcDoRun`). **Phase E** filled in the real LocalSystem service body + WTS session-aware spawning (Controller in Session 0 via `SeDebugPrivilege`, ClipboardInterceptor per session via `CreateProcessAsUser`), per-session proxy, and a self-contained deployable bundle (`package-bundle.ps1`) that installs on a clean Win11 VM. **Phase F** (admin control / drain / polish) is the only remaining phase.
4. **Phases A → E are complete.** (Originally: `orchestrator/session.py` was a 1-line stub and the LocalSystem service body + WTS session-change handling + cross-session bridge were the scope of Phase E.) Phase E shipped all of it — see the Phase E section below.

This re-plan supersedes the original plan from Phase 2 onward. It is structured as a high-level phase list; **each phase is planned in detail in a separate follow-up session**, so this file intentionally stays brief on per-step work and explicitly omits test/verification steps (per user instruction — they would be inaccurate at this resolution).

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

**Implemented and verified** (Phase 0–2 of old plan, Phase A, B, C, **and D**):
- `orchestrator/server.py` — multi-instance pipe server, accepts JSON, dispatches, writes response in accept thread. **Phase C fix #1:** explicit `SECURITY_ATTRIBUTES` with DACL granting `Authenticated Users` `FILE_GENERIC_READ | FILE_GENERIC_WRITE` so medium-integrity TransferAgent can connect to an elevated orchestrator's pipe.
- `orchestrator/dispatcher.py` — three per-channel `ThreadPoolExecutor`s (clipboard/browser/peripheral), 4 s timeout fail-closed, clipboard supersession via `_clip_seq` / `_clip_inflight`.
- `orchestrator/policy_manager.py` — `DLPEngine` wrapper, `watchdog` hot-reload with 100 ms debounce + on_modified/on_moved/on_created handlers, lock-guarded snapshot-on-entry (strict bar).
- `orchestrator/config.py` — dataclass loader for the central `config.yaml`; carries the raw parsed tree on `OrchestratorConfig.raw` for the ctl-pipe broadcaster. **Phase D additions:** `transfer_agent_exe`, `shell_extension_dll`, `payload_dll` (defaulted to `""` so existing test fixtures don't need to enumerate them).
- `orchestrator/logging_setup.py` — rotating file + console.
- `orchestrator/__main__.py` — argparse dispatch into `--foreground` / `--install` / `--uninstall` / `--service`. **Phase D fix #4:** top-level imports kept minimal; heavy modules (`policy_manager`, `dispatcher`, etc.) lazy-imported inside `_run_foreground` so `--service` can start from the embed without analyzer deps.
- `orchestrator/ctl_server.py` — **(Phase B):** single-instance ctl-pipe server, rejects duplicate subscribes with `already_subscribed`, projects per-component sections from raw config, push delivery with 500 ms write deadline.
- `orchestrator/config_watcher.py` — **(Phase B):** watchdog-based FileSystemWatcher on `config.yaml`, 200 ms debounce, parses + invokes on_change callback.
- `orchestrator/supervisor.py` — **(Phase C):** `Supervisor` + `ChildSpec` + `build_default_specs`. Spawns mitmdump / ClipboardInterceptor / Controller with `CREATE_NEW_PROCESS_GROUP`, restart watcher (max 3 crashes in 60 s, `stable_uptime_reset_seconds=60` resets the counter, "give up on this child" past cap), per-child rotating logs at `%PROGRAMDATA%\DLP\logs\supervisor-<child>.log`, `CTRL_BREAK_EVENT` shutdown (10 s grace for controller — `critical_terminate=True`), `DLP_SUPERVISOR_DISABLED` env-var opt-out for the harness.
- `orchestrator/installer.py` — **(Phase D):** transactional install/uninstall driver. `InstallContext` + `Step(id, do, undo)` + `_drive_install` + `_drive_uninstall`. 12 do/undo step factories (admin/arch checks, artifact verify, dir creation, file copy with retry + delete-on-reboot fallback, mitmproxy CA bootstrap via `CertStore.from_store` API, certutil install, HKCU proxy backup+set, HKLM ShellExt registration, `SHChangeNotify`, `sc.exe` service install). Manifest persisted after each step; rollback on failure; uninstall idempotent.
- `orchestrator/service.py` — **(Phase D placeholder):** `DLPAgentService(win32serviceutil.ServiceFramework)` with `_svc_name_=DLPAgent`. `SvcDoRun` logs CRITICAL warning + blocks on `hWaitStop`. `run_as_service` uses `PrepareToHostSingle` + `StartServiceCtrlDispatcher` (no PythonService.exe needed). Phase E replaces the body.
- `interceptors/browser/addon.py` + `pipe_client.py` + `config.py` + `ctl_pipe_subscriber.py` — addon reads central `config.yaml` (via `DLP_CONFIG_PATH` env var → walk-up + sentinel), subscribes to ctl-pipe for live config updates.
- `src/AgentCore/PipeAgentCore.cs` — real pipe client; supports a `Func<(string, int)>` provider for hot-reloadable timeout; fail-closed on any exception.
- `src/ClipboardInterceptor/ClipboardHistoryEnforcer.cs` — keeps Windows clipboard history disabled via `RegNotifyChangeKeyValue`.
- `src/ClipboardInterceptor/Program.cs` + `ClipboardConfigHolder.cs` — reads central `config.yaml` via `DlpShared.ConfigLocator`, subscribes to ctl-pipe.
- `src/DlpShared/` — **(Phase B):** shared C# library with `ConfigLocator` (env var → walk-up N=8 + sentinel check) and `CtlPipeSubscriber` (long-lived message-mode subscriber, exponential-backoff reconnect, handles `already_subscribed` retryably).
- `analyzer/cli_extractor.py` — standalone CLI for file-text extraction.

**Peripheral_storage components (Phase B integrated, Phase D installed):**
- `interceptors/peripheral_storage/Controller/Program.cs` + `Config/AppConfig.cs` — Controller reads the central `config.yaml`'s `peripheral_storage` section via `DlpShared.ConfigLocator`. The legacy FileSystemWatcher is replaced by a `CtlPipeSubscriber`; the existing selective-update logic in `TryReload(AppConfig)` (target_processes / fail_mode / payload_dll_path / `shared_memory_name` rejection) is preserved. `running-config.yaml` is retained as an audit-trail artifact.
- `interceptors/peripheral_storage/Payload/{dllmain,hook}.cpp` — injected DLL that hooks `NtCreateFile`; reads removable-drive seqlock from `Global\UsbDlpDriveMap`; deactivates on `AliveMutex` release.
- `interceptors/peripheral_storage/ShellExtension/DlpContextMenu.cpp` — COM context-menu handler ("Transfer to USB (DLP Protected)") that reads `HKLM\SOFTWARE\DLPAgent\TransferAgentPath` (Phase D moved registration from HKCU to HKLM); the existing HKLM → HKCU fallback in the C++ stayed unchanged.
- `interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs` + `Program.cs` — TransferAgent reads central `config.yaml` at startup via `DlpShared.ConfigLocator` (one-shot disk read; intentionally does NOT subscribe to ctl-pipe given its per-file lifecycle). Sends the same `{channel:"peripheral_storage", kind:"file", ...}` payload as before. **Phase C fix #2:** TransferForm row Notes are copyable via Ctrl+C and right-click context menu.
- `interceptors/peripheral_storage/verify-install.ps1` — **DEPRECATED (Phase D):** tombstone banner; exits 1 even with `DLP_ALLOW_LEGACY_INSTALL=1`. The functional install path is now `python -m orchestrator --install`.

**Phase D developer-side prep (not part of the installer itself):**
- `scripts/prepare-python-embed.ps1` — downloads Python 3.13 embeddable, patches `python313._pth` (uncomments `import site`, appends `Lib\site-packages` and `..`), bootstraps pip, installs top-level `requirements.txt`, writes `sitecustomize.py` for pywin32 DLL discovery.
- `scripts/prepare-install-payload.ps1` — `dotnet build` × 3 (Clipboard, Controller, TransferAgent) + `msbuild /p:SolutionDir=…\` × 2 (Payload, DlpShellExt). Verifies every artifact lands at the path `config.yaml` expects.
- `scripts/harness/test_installer.py` — 6 pytest cases exercising the `_drive_install` / `_drive_uninstall` rollback + manifest idempotency without touching real Win32 (synthetic step list).

**Stubs (1-line docstrings only):**
- *(none — `orchestrator/session.py` was a stub at end of Phase D; Phase E implemented it: WTS enumeration, `CreateProcessAsUser` session bridge, per-session proxy.)*

## Critical files referenced throughout this plan

- `orchestrator/server.py`, `orchestrator/dispatcher.py`, `orchestrator/policy_manager.py`, `orchestrator/config.py`
- `orchestrator/ctl_server.py`, `orchestrator/config_watcher.py` (Phase B)
- `orchestrator/supervisor.py` (Phase C), `orchestrator/installer.py` (Phase D), `orchestrator/service.py` (Phase D placeholder, Phase E body), `orchestrator/session.py` (Phase E to-build)
- `scripts/prepare-python-embed.ps1`, `scripts/prepare-install-payload.ps1`, `scripts/harness/test_installer.py` (Phase D dev-side)
- `src/DlpShared/ConfigLocator.cs`, `src/DlpShared/CtlPipeSubscriber.cs` (Phase B; referenced by AgentCore, Controller, TransferAgent)
- `interceptors/peripheral_storage/Controller/Program.cs`, `Controller/Config/AppConfig.cs` (reads `peripheral_storage` section of the central `config.yaml`)
- `interceptors/peripheral_storage/TransferAgent/OrchestratorClient.cs`, `TransferAgent/Program.cs`, `TransferAgent/TransferForm.cs` (Phase C fix #2 copy affordances)
- `interceptors/peripheral_storage/ShellExtension/DlpContextMenu.cpp`, `ShellExtension/DlpContextMenu.h` (CLSID `{B3A1C2D4-E5F6-7890-ABCD-EF1234567890}` lives here)
- `interceptors/peripheral_storage/verify-install.ps1` (Phase D tombstone)
- `interceptors/browser/config.py`, `interceptors/browser/ctl_pipe_subscriber.py` (Phase B; addon reads central `config.yaml`)
- `config.yaml` (central, at repo root; renamed from `orchestrator.yaml` in Phase B; gained `paths.transfer_agent_exe / shell_extension_dll / payload_dll` + top-level `install:` section in Phase D), `analyzer/policies.yaml`
- `python-embed/` (Phase D, gitignored, produced by `prepare-python-embed.ps1`)

---

## Phases

### Phase A — Stabilize Phase-2 work (validation + bug-fix) ✅ COMPLETED

**Goal (achieved):** prove the four flagged behaviors actually hold under stress, and fix anything that does not. No new functionality.

**Outcomes:** 7 pytests under `scripts/harness/` cover the four gaps (`test_concurrency.py` × 2 for multi-instance pipe concurrency, `test_hot_reload.py` × 2 for policy hot-reload under load, `test_supersession.py` × 1 for clipboard supersession, `test_timeout.py` × 2 for dispatcher fail-closed timeout). Plus 2 AgentCore tests for the C# pipe-client side. All pass. The harness fixture (`scripts/harness/conftest.py:make_orchestrator`) spawns an isolated orchestrator subprocess per test with a unique pipe name + private policies/config under `tmp/harness/<uuid>/` and `CREATE_NEW_PROCESS_GROUP` so `CTRL_BREAK_EVENT` can drive clean shutdown.

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

### Phase C — Supervisor: spawn, watch, restart all four child processes (foreground mode) ✅ COMPLETED

**Goal (achieved):** `python -m orchestrator --foreground` starts the orchestrator **and** launches/supervises mitmdump, ClipboardInterceptor.exe, and Controller.exe. Crashes restart per policy (max 3 in 60 s window). Ctrl-C kills children cleanly. TransferAgent is NOT supervised — it is launched on demand by ShellExtension and must never be hooked (per memory).

**Outcomes (locked decisions from the Phase C planning session — full doc at `phase-c-plan.md`):**
- **Shutdown signal: `CTRL_BREAK_EVENT`** directed at each child's own process group (spawn with `CREATE_NEW_PROCESS_GROUP` so `proc.send_signal(CTRL_BREAK_EVENT)` works on Windows). Triggers the existing `Console.CancelKeyPress` handlers in Controller and ClipboardInterceptor verbatim.
- **Per-child rotating log files** under `%PROGRAMDATA%\DLP\logs\supervisor-<child>.log` (5 MB × 3 backups). Logger name `orchestrator.supervisor.<child>` with `propagate=False` so child output doesn't double-write into `dlp-agent.log`.
- **Past the restart cap, give up on the offending child; other children stay supervised.** Loud ERROR log line; orchestrator does not panic-stop.
- **Per-child grace windows:** 5 s for mitmdump and clipboard, 10 s for controller (the WMI watcher in `ProcessMonitor.Dispose()` can stall 15–30 s, but `Controller/Program.cs:190` releases the alive mutex *before* disposal — so the hook-deactivation part of teardown happens within milliseconds). Forced kill of controller logs at CRITICAL because hooks stay live in `explorer.exe` until Explorer restart.
- `DLP_SUPERVISOR_DISABLED=1` env-var opt-out so the harness can spawn an orchestrator without the three real children.

**Post-implementation fixes:**
- **Fix #1 — data-pipe DACL.** Running the supervised orchestrator from an elevated shell (required for `SeDebugPrivilege` cross-session injection) split the integrity level between orchestrator (high) and ShellExt-launched TransferAgent (medium). Default named-pipe DACL gave Everyone READ but not WRITE, so TransferAgent's `NamedPipeClientStream(PipeDirection.InOut)` got `ERROR_ACCESS_DENIED`. Fix: explicit `SECURITY_ATTRIBUTES` in `server.py` granting `SYSTEM` + `Administrators` `FILE_ALL_ACCESS` and `Authenticated Users` `FILE_GENERIC_READ | FILE_GENERIC_WRITE`. ctl_pipe stays on default DACL (no cross-integrity clients today; Phase E will tighten it to Administrators-only).
- **Fix #2 — TransferForm copy affordances.** Ctrl+A / Ctrl+C and a right-click context menu (`Copy` / `Copy Note`) so operators can copy BLOCK reason text out of the TransferForm grid.

**Verification:** the supervisor smoke (3 pytests in `test_supervisor.py`) covers spawn-lifecycle, missing-exe-raises, and stop_all-idempotency. Manual end-to-end smoke confirmed crash respawn, Ctrl+C graceful shutdown (controller releases mutex within 1 s of BREAK, hooks deactivate), restart-cap give-up, and stable-uptime reset.

### Phase D — Installer / Uninstaller (replaces `verify-install.ps1` and standalone setup) ✅ COMPLETED

**Goal (achieved):** one `python -m orchestrator --install` stands up the full endpoint; one `--uninstall` reverses everything idempotently.

**Outcomes (locked decisions from the Phase D planning session — full doc + post-impl fixes at `C:\Users\PocketBaguette\.claude\plans\code-base-brief-this-delightful-flamingo.md`):**
- **Pre-built artifacts.** Installer reads paths from `config.yaml`'s `paths:` section, resolves against the dev tree root, fails loudly with absolute paths if any are missing. Build is a developer concern — `scripts/prepare-install-payload.ps1` does `dotnet build` × 3 + `msbuild /p:SolutionDir=…\` × 2.
- **HKLM machine-wide ShellExt registration** (CLSID + `*\shellex\…` + `Directory\shellex\…` + `Approved\…` + `SOFTWARE\DLPAgent\TransferAgentPath`). Matches the Phase E LocalSystem-service model; one registration covers every user-session `explorer.exe`. Existing `DlpContextMenu.cpp:255-258` HKLM-then-HKCU fallback meant no C++ changes needed.
- **Service registered, body is a placeholder.** `sc create DLPAgent binPath= "…python.exe -m orchestrator --service --config …"`. `SvcDoRun` writes a CRITICAL warning to the event log and blocks on `hWaitStop`. Phase E replaces the body. Operators run `python -m orchestrator --foreground` from the source-tree dev `.venv` for actual DLP work until Phase E lands.
- **Python 3.13 embeddable bundled at `<install_root>\python\`.** `scripts/prepare-python-embed.ps1` downloads + patches `python313._pth` (uncomments `import site`, appends `Lib\site-packages` and `..` — the `..` is load-bearing for SCM-launched `python.exe -m orchestrator` to find the package, see Fix #3 below). Bootstraps pip, installs the top-level `requirements.txt`, writes a `sitecustomize.py` that calls `os.add_dll_directory(<embed>/Lib/site-packages/pywin32_system32)` so pywin32 DLLs are findable on the embed.
- **Install layout** under `%ProgramFiles%\DLP\`: `python\`, `orchestrator\`, `analyzer\`, `interceptors\browser\`, `bin\{Controller,Clipboard,TransferAgent,ShellExt}\`, `config.yaml` (paths rewritten to install-relative). `%ProgramData%\DLP\state\` holds `install_manifest.json`, `installed_ca.txt`, `proxy_backup.json`. `%ProgramData%\DLP\logs\` holds `dlp-agent.log` + per-child `supervisor-*.log`. `%ProgramData%\DLP\mitmproxy\` holds the generated CA.
- **mitmproxy CA bootstrap via `CertStore.from_store` API**, not via `mitmdump` CLI (see Fix #2). `certutil -addstore -f Root <cer>` installs to LocalMachine\Root; thumbprint recorded in `%ProgramData%\DLP\state\installed_ca.txt`.
- **HKCU proxy** backed up to `proxy_backup.json` before `ProxyEnable=1`, `ProxyServer=127.0.0.1:8080`, `ProxyOverride=<config.proxy_bypass>`. Phase E extends to other sessions via `RegLoadKey HKU\<SID>`.
- **Transactional driver + manifest persistence.** Each step is a `(do, undo)` pair; on any failure the driver runs undos in reverse over completed steps. `--uninstall` runs every step's undo regardless of whether `do` ran in this session; missing-target errors are logged at INFO and continued past. Manifest written after each successful step so a crash between steps still leaves enough data to uninstall.
- **`verify-install.ps1` deprecated as a tombstone** (prints redirect banner, exits 1 even with `DLP_ALLOW_LEGACY_INSTALL=1`). Recovery via git history.

**Post-implementation fixes:**
- **Fix #1 — DlpShellExt `$(SolutionDir)` build path.** Initial `prepare-install-payload.ps1` invoked msbuild without `/p:SolutionDir`, so the DLL landed at `ShellExtension\out\ShellExtension\<Config>\DlpShellExt.dll` (project-local default) instead of `interceptors\peripheral_storage\out\ShellExtension\<Config>\DlpShellExt.dll`. Fix: split the C++ build loop, pass `/p:SolutionDir=…interceptors\peripheral_storage\` (trailing backslash mandatory) for DlpShellExt only.
- **Fix #2 — bootstrap_ca via `CertStore.from_store` API instead of mitmdump CLI.** mitmproxy 12.2.3's mitmdump CLI no longer triggers `tls_config.configure()` (which calls `CertStore.from_store` to write the CA files) for either `--no-server` or `--listen-port 0`. Empirically: mitmdump exits clean with code 0 and no CA files. Fix: invoke `mitmproxy.certs.CertStore.from_store(path, 'mitmproxy', 2048, None)` via `python -c` — synchronous, no port binding, no event loop.
- **Fix #3 — `..` in `python313._pth`.** Embeddable Python is in isolated mode when `_pth` is present: only paths in `_pth` are on sys.path, the current directory is NOT added. SCM launches the service from `C:\Windows\System32`, so `python.exe -m orchestrator` failed with `No module named orchestrator` (the package lives at `<install_root>\orchestrator\`, not `<install_root>\python\`). Fix: add `..` to `_pth` so `<install_root>\` is on sys.path regardless of cwd.
- **Fix #4 — Lazy mode-specific imports in `__main__.py`.** Top-level `from orchestrator.policy_manager import PolicyManager` cascaded into `import ahocorasick`, but the embed's `python-embed\Lib\site-packages` only has the top-level `requirements.txt` deps (`mitmproxy`, `pywin32`, `pyyaml`, `watchdog`). The analyzer's heavy deps (`pyahocorasick`, `google-re2`, `PyMuPDF`, etc.) live in `analyzer/requirements.txt` and were never bundled. So `python -m orchestrator --service` from the install died with `ModuleNotFoundError` before SCM dispatch. Fix: defer heavy imports inside `_run_foreground`; `--service` / `--install` / `--uninstall` paths import only what they need.

**Verification (all green):** 6 new pytest cases in `scripts/harness/test_installer.py` cover forward success, midway-failure rollback, manifest-driven uninstall, synthesized uninstall, idempotency, and single-step failure rc=1. Total harness: 18/18 passing. Manual end-to-end smoke on the dev VM: install + `sc query DLPAgent` (registered) + context-menu round-trip (ALLOW for clean file, BLOCK for CCCD-bearing file via `--foreground` orchestrator in another shell) + `sc start DLPAgent` (placeholder RUNNING) + `sc stop DLPAgent` (clean) + `--uninstall` (everything reversed) + second `--uninstall` (idempotent no-op).

**Known limitation carried into Phase E:** the bundled `python-embed` does NOT include analyzer deps (Phase D fix #4 layer B). So `python -m orchestrator --foreground` from the *installed* `%ProgramFiles%\DLP\` won't work; only the placeholder service runs cleanly there. Operators use the dev `.venv` for actual DLP enforcement until Phase E layer B ships (add `pip install -r analyzer\requirements.txt` to `prepare-python-embed.ps1`, accept ~200+ MB embed size).

### Phase E — LocalSystem service + Session-aware spawning + deployable bundle ✅ COMPLETED

**Goal (achieved):** orchestrator runs as the `LocalSystem` `DLPAgent` service, spawns interceptors into the correct sessions, follows logon/logoff, restarts crashed children, and delivers real DLP decisions. The open process-context question is answered empirically. A self-contained **deployable bundle** lets a clean Win11 VM install with no Visual Studio / Developer PowerShell / dotnet. Full detail + the nine post-implementation fixes are in `C:\Users\PocketBaguette\.claude\plans\code-base-brief-this-functional-lagoon.md`.

**Outcomes (locked decisions from the Phase E planning + VM-deployment sessions):**
- **Process-context question RESOLVED empirically (spike on Win11 26200): Option A.** A Session-0 LocalSystem Controller with `SeDebugPrivilege` enabled (new `Controller/Privileges.cs`) cross-session-injects `Payload.dll` into every logged-on session's `explorer.exe` — confirmed across two fast-user-switch sessions. Controller stays a single Session-0 `subprocess.Popen` child. `config.yaml peripheral_storage.controller_in_user_session` (default `false`) flips it to the per-session + elevated-linked-token fallback (B), which stays wired but unused.
- **All logged-on sessions supported.** `orchestrator/supervisor.py` gained a `(session_id, child)` table + `start_session`/`stop_session`; `service.SvcOtherEx` routes `WTS_SESSION_LOGON`/connect → spawn + per-session proxy, `WTS_SESSION_LOGOFF` → teardown + proxy restore. `ClipboardInterceptor` runs per interactive session via `CreateProcessAsUser` (`orchestrator/session.py`, `lpDesktop=winsta0\default`); `mitmdump` + `Controller` are single Session-0 children. Foreground (`--foreground`) keeps the Phase C behavior — all new machinery is gated on `service_mode`.
- **Real service body.** `__main__.run_core(config_path, stop_event, *, foreground)` is shared by `--foreground` (Ctrl+C → stop_event) and the service (`SvcStop` → stop_event); `service.SvcDoRun` runs it with `GetAcceptedControls |= SERVICE_ACCEPT_SESSIONCHANGE`. Start type stays `demand` (Q-E4; Phase F may flip to `auto`).
- **Per-session HKCU proxy** via `HKEY_USERS\<SID>` (the logged-on user's hive is already mounted — no `RegLoadKey`), backed up per-SID under `state_dir`, restored at logoff.
- **Analyzer deps bundled** into the embed (`prepare-python-embed.ps1`) — `pyahocorasick` is copied pre-compiled from the dev `.venv` because the embeddable Python can't compile C extensions.
- **Deployable bundle** (new `scripts/package-bundle.ps1` + `installer.build_bundle_config`): a lean self-contained `dist\DLP\` + `dist\DLP.zip` installs on a clean VM via `install.cmd`/`uninstall.cmd`, running entirely on the bundled embed Python + built-in `certutil`/`sc`/`reg`. VM prerequisite is the **.NET 10 Desktop Runtime**; the native DLLs are statically linked so no VC++ redist is needed.

**Post-implementation fixes (PF#1–#9; full detail in the plan doc):** (1) `sc`-alias/stale completion message; (2) pyahocorasick-into-embed; (3) deployable bundle; (4) **.NET 10 Desktop Runtime VM prerequisite** (framework-dependent .NET apps); (5) mitmdump pip-launcher hardcoded host path → launch via `sys.executable -c` shim + `--set confdir` for the installed CA; (6) **native DLLs → static CRT** (`Payload`/`DlpShellExt` `/MT(d)`, detours `x64-windows-static`) so they load with no VC++ debug runtime; (7) **browser block popup via `WTSSendMessageW`** (Session-0 → active-console desktop) instead of `MessageBoxW`; (8) benign session-change log noise (1008 connect-before-logon → INFO; service thread named `svc-main`); (9) **uninstall removes the install tree synchronously** — `uninstall.cmd` runs the bundle's python (no self-lock) and `_rmtree_with_retry` moves locked DLLs aside (same-volume rename) instead of relying on reboot or killing explorer.

**Verification (all green):** harness 25/25 (incl. `scripts/harness/test_session.py` ×6 + a `build_bundle_config` case). Manual end-to-end on a clean Win11 26200 VM: install via `install.cmd`; `Start-Service DLPAgent`; clipboard/USB/browser BLOCK+ALLOW decisions; USB write blocked + "Transfer to USB (DLP Protected)" menu + TransferAgent; browser block popup on the user desktop; admin **and** non-admin fast-user-switch sessions each get their own children + proxy; `uninstall.cmd` removes everything with no leftover folder and no reboot.

**Done when (achieved):** install + logon → mitmdump + per-session ClipboardInterceptor + Session-0 Controller (injecting every session's `explorer.exe`) + on-demand ShellExtension-launched TransferAgent all run in the correct contexts; logoff cleans up per session; `Stop-Service DLPAgent` drains cleanly; uninstall is clean + idempotent; the service body delivers real DLP decisions.

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

Resolved through Phase D; open ones surface in Phase E / F:

1. ~~**Phase B:** Controller reads `orchestrator.yaml` directly, or installer-synced `config.yaml`?~~ **RESOLVED:** Controller reads central `config.yaml` directly via `DlpShared.ConfigLocator`. No shadow file.
2. ~~**Phase B:** Does Controller's own `FileSystemWatcher` hot-reload stay, or is hot-reload centralised in the orchestrator?~~ **RESOLVED:** Centralised. Controller subscribes to the orchestrator's ctl-pipe and re-applies its existing selective-update logic on each push. The FileSystemWatcher on the legacy local file is removed.
3. ~~**Phase C:** Shutdown signal mechanism for Controller (CTRL_BREAK, named event, or other)?~~ **RESOLVED:** `CTRL_BREAK_EVENT` directed at each child's own process group (children spawned with `CREATE_NEW_PROCESS_GROUP`). Triggers the existing `Console.CancelKeyPress` handlers verbatim.
4. ~~**Phase C:** Per-child log streams vs. interleaved logging?~~ **RESOLVED:** Per-child rotating log files at `%PROGRAMDATA%\DLP\logs\supervisor-<child>.log` with `propagate=False` so they don't double-write into `dlp-agent.log`.
5. ~~**Phase D:** Build during install vs. expect pre-built?~~ **RESOLVED:** Pre-built. Installer reads paths from `config.yaml`, fails loudly with absolute paths if missing. `scripts/prepare-install-payload.ps1` is the dev-side builder.
6. ~~**Phase D:** ShellExtension registration in HKCU (current, no-admin) vs HKLM (machine-wide)?~~ **RESOLVED:** HKLM machine-wide. Matches the Phase E LocalSystem-service model; the C++ `DlpContextMenu.cpp:255-258` HKLM-then-HKCU fallback meant no native source change needed.
7. ~~**Phase E (the big one):** Controller process context — LocalSystem cross-session injection, CreateProcessAsUser, or Task Scheduler?~~ **RESOLVED (empirically, Win11 26200): Option A.** Session-0 LocalSystem Controller with `SeDebugPrivilege` (`Controller/Privileges.cs`) cross-session-injects into every session's `explorer.exe`; spike confirmed across two fast-user-switch sessions. Fallback B (per-session + elevated linked token) is wired behind `config.yaml peripheral_storage.controller_in_user_session` but unused.
8. ~~**Phase E:** Multi-session support — single active session or all logged-on?~~ **RESOLVED:** all logged-on sessions. Supervisor `(session_id, child)` table + `start_session`/`stop_session`; `service.SvcOtherEx` drives spawn/teardown + per-session proxy on logon/connect/logoff.
9. ~~**Phase E:** Analyzer deps in the bundled embed vs vendored wheelhouse?~~ **RESOLVED:** bundled in the embed via `prepare-python-embed.ps1` (`pyahocorasick` copied pre-compiled from the dev `.venv` — the embed can't compile C extensions). Embed grew to the expected ~200–400 MB; fine on the test VM.
10. ~~**Phase E:** Service start type — `demand` or `auto`?~~ **RESOLVED (for Phase E):** stays `start= demand`; revisit `auto` in Phase F now that the real body is proven.
11. **Phase F:** Should `dlp-ctl status` reach into Supervisor's `status_snapshot()` via the ctl-pipe (clean) or via a separate admin-only IPC channel (more isolated)?
12. **Phase F (new, deferred from Phase E):** real-endpoint runtime delivery — currently the VM needs the **.NET 10 Desktop Runtime** installed and relies on the bundled static-CRT native DLLs. For mass deployment decide between self-contained .NET publish (Option A) vs bundling the runtime installer with silent auto-install (Option C).
