# Phase E — LocalSystem service + session-aware spawning + deployable bundle

> **Cross-reference key**
> - **E0 … E7** — the core implementation tasks (all **DONE**; E0 was the spike gate).
> - **Q-E1 … Q-E4** — user-confirmed design decisions.
> - **PF#1 … PF#7** — post-implementation fixes found while deploying to the test VM (each its own section below; tagged **DONE** or **TODO**).
> - **R1 … R9** — tracked risks.
> - **Glossary.** *Session 0* = the non-interactive session where Windows services run (our LocalSystem `DLPAgent`). *Interactive session* = a logged-on user's session (console/RDP) that owns a window-station/desktop/clipboard. *WTS* = Windows Terminal Services API (`win32ts`) for enumerating sessions + getting user tokens. *Cross-session injection* = a Session-0 process writing a remote thread into a process in an interactive session. *`install_root`* = `%ProgramFiles%\DLP`; *`state_dir`* = `%ProgramData%\DLP\state`.

## Context

End of Phase D: `python -m orchestrator --install` stands up `%ProgramFiles%\DLP\` with a registered **but placeholder** `DLPAgent` service; `orchestrator/session.py` is a stub. Phase E makes the service do the real work: run as LocalSystem, spawn the interceptors into the correct sessions, follow logon/logoff, and deliver real DLP decisions. It also adds a **deployable bundle** so a clean Win11 VM (no Visual Studio / Developer PowerShell / dotnet) can install and run the agent.

**Why a session bridge is unavoidable:** `ClipboardInterceptor` listens via `AddClipboardFormatListener` on a message-only window (`src/ClipboardInterceptor/ClipboardMonitor.cs:13-41`); the clipboard/window-station are per interactive session, so a Session-0 service can't see them. It must be launched into each session via `CreateProcessAsUser`.

## Locked decisions

| # | Decision | Outcome |
|---|---|---|
| Q-E1 | Spike "Option A" (Controller in Session 0, `SeDebugPrivilege`, cross-session injection) before committing. | ✅ **Spike PASSED.** Controller stays a single Session-0 child and injects every session. `config.yaml peripheral_storage.controller_in_user_session: false`. Fallback B (per-session + linked token) stays wired but unused. |
| Q-E2 | Support **all** logged-on sessions (RDP + fast-user-switch). | Supervisor keeps a `(session_id, child)` table; spawn on logon, tear down per session on logoff; per-session proxy for each. |
| Q-E3 | Bundle analyzer deps into the embed. | `prepare-python-embed.ps1` installs `analyzer/requirements.txt` (see PF#2). |
| Q-E4 | Service start type stays `demand` this phase. | Flip to `auto` in Phase F once proven. |

## Architecture — where each child runs

| Child | Session | Spawn | Notes |
|---|---|---|---|
| `mitmdump` | Session 0 | `Popen` of `sys.executable -c <shim>` (see PF#5) | One instance; binds `127.0.0.1:8080`; all sessions reach it via per-user proxy keys. |
| `ClipboardInterceptor` | every interactive session | `CreateProcessAsUser` (plain user token) | Window-station-bound. |
| `Controller` | Session 0 | `Popen` | Cross-session injects Payload into each session's `explorer.exe`. |
| `TransferAgent` | user session, on demand | launched by ShellExt inside `explorer.exe` | Never hooked (excluded from `target_processes`). |

## Critical files

**New:** `orchestrator/session.py` (WTS enumeration, `CreateProcessAsUser`, per-session proxy); `scripts/harness/test_session.py`; `interceptors/peripheral_storage/Controller/Privileges.cs` (`SeDebugPrivilege`); `scripts/package-bundle.ps1` (PF#3).

**Edited:** `orchestrator/service.py` (real `SvcDoRun` + `GetAcceptedControls` + `SvcOtherEx`); `orchestrator/__main__.py` (`run_core` shared by `--foreground`/`--service`); `orchestrator/supervisor.py` (session-aware table + `start_session`/`stop_session`); `orchestrator/installer.py` (`build_bundle_config`, message); `Controller/{NativeMethods.cs, Program.cs}`; `scripts/prepare-python-embed.ps1`; `config.yaml`.

**Reused unchanged:** `orchestrator/server.py` data-pipe DACL (Phase C fix #1) lets cross-session children connect; all `Global\UsbDlp*` objects are already cross-session-capable.

## Implementation tasks (all DONE)

- **E0** — Injection spike. ✅ PASS → Option A (above).
- **E1/PF#2** — Analyzer deps in embed (see PF#2).
- **E2** — `session.py`: `enumerate_interactive_sessions`, `user_token_for_session` (+`linked_token`), `sid_for_token`, `spawn_as_user` (`CreateProcessAsUser`, `lpDesktop=winsta0\default`), `set/restore_session_proxy` (via `HKEY_USERS\<SID>`). Verified: all pywin32 attrs exist; imports clean.
- **E3** — Supervisor: `ChildSpec.session_scope`/`needs_elevation`; Session-0 dict + `(session_id,name)` dict; `start_session`/`stop_session`; foreground keeps Phase-C behavior (service-only machinery gated on `service_mode`).
- **E4** — `run_core(config_path, stop_event, *, foreground)` shared by `--foreground` (Ctrl+C→stop_event) and the service (`SvcStop`→stop_event). `service.py` real body: `SvcDoRun`→`run_core`; `GetAcceptedControls |= SERVICE_ACCEPT_SESSIONCHANGE`; `SvcOtherEx` routes logon/connect→`start_session`, logoff→`stop_session`.
- **E5** — Per-session proxy set on logon/`start_all`-seed, restored on logoff (inside `start_session`/`stop_session`).
- **E6** — `scripts/harness/test_session.py` (fake bridge: table bookkeeping, idempotent logon, per-session logoff, restart-on-crash, linked-token). **Harness 25/25 green.**
- **E7** — Manual VM smoke (in progress — see PF#4/#6/#7).

## Risks

- **R1** Cross-session injection viability → RESOLVED (spike passed).
- **R2** `CreateProcessAsUser` desktop/STA correctness → `lpDesktop=winsta0\default`; clipboard works on VM. ✅
- **R3** Service-mode children can't get `CTRL_BREAK` (no Session-0 console) → terminate + AliveMutex-release deactivates Payload hooks; validate abandoned-mutex branch during smoke.
- **R4** Embed size growth from analyzer deps → acceptable on 32 GB-free VM.
- **R5** Fallback-B linked token absent for standard users → only relevant if spike had failed; it didn't.
- **R6** Install-time HKCU proxy vs runtime per-session proxy → service is runtime authority; per-SID backup files.
- **R7** Dev `--foreground` + installed service both binding pipes → startup WARN; don't run both.
- **R8** Bundle prep depends on host `.venv` (compiled `pyahocorasick`, `build_bundle_config`) → prep scripts error clearly if absent; VM needs only the bundle.
- **R9** Bundle config drift → `build_bundle_config` copies verbatim except the rewritten keys; pinned by a `test_installer.py` case.

---

# Post-implementation fixes

> Found while deploying/testing on the clean Win11 VM. PF#1–#5 are **DONE**; PF#6–#7 are **TODO** (this session). After PF#6/#7 land: rebuild artifacts, repackage, redeploy, finish E7.

## Post-implementation fix #1 — `sc` alias + stale install message  **[DONE]**

### Symptom
After install, `sc start DLPAgent` / `sc query DLPAgent` produced **no output** and "no process ran."

### Root cause
In PowerShell, **`sc` is an alias for `Set-Content`**, not `sc.exe`. The commands silently wrote files named `start`/`query`; the service was never touched. The installer's completion log also still said "placeholder service / idle until Phase E," reinforcing the confusion.

### Fix
- Use `Start-Service DLPAgent` (or `sc.exe start DLPAgent`); query `Get-Service DLPAgent`.
- `orchestrator/installer.py` completion message rewritten to the real-agent text + `Start-Service` + the `sc` alias warning.

### Verification
`Start-Service DLPAgent` → `Get-Service DLPAgent` shows **Running**; `dlp-agent.log` shows the real body (pipes bound, PolicyManager loaded). ✅ Confirmed on VM.

## Post-implementation fix #2 — analyzer deps in the embed (pyahocorasick)  **[DONE]**

### Symptom
The real service body imports `analyzer.engine` → needs `pyahocorasick`/`google-re2`/`PyMuPDF`/office libs, which Phase D's embed omitted. Naïvely adding `pip install -r analyzer/requirements.txt` to the embed **aborts on `pyahocorasick`**.

### Root cause
The embeddable Python ships **no `Include/` or `libs/`**, so it can't compile C extensions; `pyahocorasick` has no cp313 Windows wheel (source-only). `google-re2`, `PyMuPDF`, and the office libs all ship cp313 wheels and install fine.

### Fix
`scripts/prepare-python-embed.ps1`: **before** the analyzer `pip install`, copy the already-compiled `ahocorasick*.pyd` + `pyahocorasick-*.dist-info` from `<RepoRoot>\.venv\Lib\site-packages` into the embed's `Lib\site-packages` (errors clearly if absent). pip then sees it satisfied and skips the build. (cp313 ABI is stable across 3.13.x.) Smoke import extended to `ahocorasick, re2, fitz`.

### Verification
`& "<embed>\python.exe" -c "import ahocorasick, re2, fitz; print('ok')"` → `analyzer deps OK`. ✅ Confirmed on VM.

## Post-implementation fix #3 — deployable bundle  **[DONE]**

### Symptom / need
Installing on the VM meant copying the whole multi-GB dev tree and hand-editing host-absolute config each time.

### Fix
- `orchestrator/installer.py: build_bundle_config(src, dest)` — writes a VM-ready `config.yaml`: `paths.*`→`bin/...` (shared `_INSTALL_LAYOUT_PATHS` with the installed-config writer; `mitmdump_exe`→`python-embed/Scripts/mitmdump.exe`), `policies_file`→`analyzer/policies.yaml`, **`browser.temp_dir`→""** (only host-absolute setting), `install.install_root`→"". Pinned by a `test_installer.py` case.
- `scripts/package-bundle.ps1` — assembles a lean `dist\DLP\` (robocopy `orchestrator`/`analyzer`/`interceptors\browser`/`python-embed` + `bin\*` publish dirs, excludes `__pycache__`/`*.pyc`), writes the bundle config, drops `install.cmd`/`uninstall.cmd`/`README-DEPLOY.txt`, and `Compress-Archive`s `dist\DLP.zip`.
- Install model: bundle installs **into `%ProgramFiles%\DLP`** via the existing installer (which runs entirely on the embed Python + built-in `certutil`/`sc`/`reg`); no installer rewrite needed because the bundle is laid out as the install tree.

### Verification
`package-bundle.ps1` parses; `build_bundle_config` output verified (paths→`bin/...`, `temp_dir==""`, `install_root==""`). Full install on VM succeeded end-to-end. ✅

## Post-implementation fix #4 — .NET 10 runtime missing on the clean VM  **[DONE — Option B]**

### Symptom
After `Start-Service`, `controller` and `clipboard` crash-loop and are given up. `dlp-agent.log` shows exit `0x80008096`; `supervisor-controller.log`: *"You must install or update .NET … Framework 'Microsoft.NETCore.App' version '10.0.0'"* (VM had only 8.0.25).

### Root cause
`Controller.csproj` / `ClipboardInterceptor.csproj` / TransferAgent are **framework-dependent** `.NET 10` (no `<SelfContained>`); the clean VM lacks the .NET 10 Desktop Runtime.

### Fix (chosen: B)
Install the **.NET 10 Desktop Runtime (x64)** on the VM once (`windowsdesktop-runtime-10.0.x-win-x64.exe /install /quiet /norestart`). No code/build change; bundle stays lean. (Options A = self-contained publish, C = bundle+auto-install were the alternatives; C is the future turnkey path for real endpoints.)

### Verification
`dotnet --list-runtimes` shows `Microsoft.WindowsDesktop.App 10.x`; clipboard now runs (confirmed). ✅

## Post-implementation fix #5 — mitmdump pip launcher hardcodes host path (+ CA confdir)  **[DONE]**

### Symptom
`mitmdump` crash-loops `exit=1` with an **empty** `supervisor-mitmdump.log`.

### Root cause
`mitmdump.exe` is a pip console-script launcher whose embedded shebang is the **host** interpreter path (`#!D:\Code\…\python-embed\python.exe`). pip launchers hardcode the install-time interpreter, so once the embed is relocated to the VM's `C:\Program Files\DLP\python`, the launcher can't find python and dies silently.

### Fix
`orchestrator/supervisor.build_default_specs`: launch mitmdump as `exe=sys.executable`, `args=["-c", _MITMDUMP_SHIM, "-s", <addon>, "--listen-port", <port>, "--set", f"confdir={<mitm_confdir>}"]` where `_MITMDUMP_SHIM = "from mitmproxy.tools.main import mitmdump; mitmdump()"`. `sys.executable` is the embed python under the service and the `.venv` python in `--foreground` — both have mitmproxy. **Also adds `--set confdir=%ProgramData%\DLP\mitmproxy`** so mitmdump uses the CA the installer placed in LocalMachine\Root (otherwise mitmdump mints an untrusted CA in the LocalSystem profile → browser HTTPS interception breaks).

### Verification
`<embed>\python.exe -c "<shim>" --version` → `Mitmproxy 12.2.1`; `build_default_specs` produces `sys.executable -c <shim> … --set confdir=…`; harness 25/25. (On-VM mitmdump start to be re-confirmed after redeploy.)

## Post-implementation fix #6 — native C++ DLLs use the non-redistributable debug CRT  **[DONE — code; rebuild+redeploy to confirm on VM]**

### Symptom
On the clean VM: USB writes are **not blocked** (copy freely via Explorer) and the **"Transfer to USB (DLP Protected)"** context menu / TransferAgent never appear.

### Root cause
`Payload.vcxproj:58` and `DlpShellExt.vcxproj:61` set `RuntimeLibrary = MultiThreadedDebugDLL` (`/MDd`). Debug builds link the **debug CRT** (`vcruntime140d.dll`, `msvcp140d.dll`, `ucrtbased.dll`) which Microsoft does **not** redistribute (it exists only where VS is installed). On the clean VM:
- `Payload.dll` fails to load when injected → no `NtCreateFile` hook → USB writes pass.
- `DlpShellExt.dll` fails to load in `explorer.exe` → no context-menu handler → no TransferAgent.

(The Controller itself runs — .NET, PF#4 — and attempts injection; the remote `LoadLibraryW` of Payload just fails for the missing CRT.)

### Fix (chosen: static CRT, keep Debug)
Make the native DLLs self-contained so the VM needs no C++ runtime:
- **`DlpShellExt.vcxproj`** — `RuntimeLibrary`: Debug → `MultiThreadedDebug` (`/MTd`), Release → `MultiThreaded` (`/MT`). No vcpkg deps → trivial.
- **`Payload.vcxproj`** — same `RuntimeLibrary` change, **plus** add `<VcpkgTriplet>x64-windows-static</VcpkgTriplet>` to the existing `<PropertyGroup Label="Vcpkg">` so `detours` is rebuilt with the static CRT (avoids `LNK2038` RuntimeLibrary mismatch — detours is currently the dynamic `x64-windows` triplet under `vcpkg_installed\x64-windows`). First build re-resolves `detours:x64-windows-static` (slower once).
- Rebuild via `prepare-install-payload.ps1`, repackage (`package-bundle.ps1`), redeploy.

### Verification
- **Built locally (this session):** both `.vcxproj` edited; `DlpShellExt.dll` and `Payload.dll` rebuilt Debug via MSBuild — exit 0, no `LNK2038` (vcpkg resolved `detours:x64-windows-static`). Binary scan confirms **neither DLL imports `ucrtbased.dll` / `vcruntime140d.dll` / `msvcp140d.dll`** anymore (CRT statically linked). `Payload.dll` 133 KB → 1.3 MB (expected).
- **On VM, after redeploy:** `tasklist /m Payload.dll` lists `explorer.exe`; right-click → **"Transfer to USB (DLP Protected)"** appears → TransferForm opens; Explorer copy to a removable drive is **blocked**; CCCD file via TransferAgent → **BLOCK**, clean → **ALLOW**.

### Risks
- Static CRT inside an injected DLL + a shell extension is the **recommended** pattern (isolates from the host process's CRT) — low risk. The only cost is the one-time `x64-windows-static` detours rebuild and slightly larger DLLs.

## Post-implementation fix #7 — browser block popup invisible from Session 0  **[DONE — code; redeploy to confirm on VM]**

### Symptom
On the VM the browser channel **blocks** uploads (e.g. CCCD via Gmail/Drive) but **no popup** appears; on the dev box (`--foreground`) the popup showed.

### Root cause
`interceptors/browser/addon.py:_notify_blocked` (`:1082-1098`) shows the notice with `ctypes.windll.user32.MessageBoxW` from inside mitmdump. Under the service mitmdump runs in **Session 0**, so the box renders on the invisible Session-0 desktop. (Interactive Services Detection / UI0Detect was removed in Win10 1803+, so it never reaches the user. The block still happens via `flow.kill()`.)

### Fix
Deliver the notice to the interactive desktop instead of the Session-0 one. Replace the `MessageBoxW` call in `_notify_blocked._show` with `WTSSendMessageW` (`wtsapi32.dll`) targeting the active console session:
```python
session_id = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
response = ctypes.c_ulong(0)
ctypes.windll.wtsapi32.WTSSendMessageW(
    0,                      # WTS_CURRENT_SERVER_HANDLE
    session_id,
    title, len(title)*2,    # byte lengths for wide strings
    msg,   len(msg)*2,
    0x40 | 0x1000,          # MB_ICONINFORMATION | MB_TOPMOST
    0,                      # no timeout
    ctypes.byref(response),
    False,                  # don't wait for the user
)
```
`WTSSendMessageW` renders on the specified session's desktop from a Session-0 service; in `--foreground` the active console session is the user's, so it still works there. Verify `WTSGetActiveConsoleSessionId` / `WTSSendMessageW` signatures during implementation.

### Verification
- **Locally (this session):** `addon.py` compiles; `WTSSendMessageW` (wtsapi32) + `WTSGetActiveConsoleSessionId` (kernel32) resolve via ctypes; harness 25/25.
- **On VM, after redeploy:** upload a CCCD-bearing file via Gmail/Drive in the user session → the "Upload Blocked by DLP" box appears **on the user desktop**; clean upload proceeds with no box.

### Risks
- `WTSSendMessageW` targets the **active console** session; for a non-active RDP/background session the notice still lands on the active console (the shared Session-0 mitmdump can't easily map a proxied connection back to its originating session). Acceptable for a notification. If exact-session routing is later needed, spawn a tiny notifier via `session.spawn_as_user` into the originating session.

## Post-implementation fix #8 — benign session-change log noise  **[DONE — code; visible after redeploy]**

### Symptom
Functionally everything works across admin + non-admin sessions, but the log has two worrying-looking lines:
1. Service-body lines are tagged `[Dummy-1]`.
2. On a fresh logon to a new session: two `ERROR … no user token for session N: (1008, 'WTSQueryUserToken', 'An attempt was made to reference a token that does not exist.')` lines, immediately followed by a **successful** spawn.

### Root cause
1. **`[Dummy-1]`** — the service body runs on the thread pywin32's `StartServiceCtrlDispatcher` created (not a `threading.Thread`). `logging`'s `%(threadName)s` resolves it via `threading.current_thread()`, which fabricates a `_DummyThread` named `Dummy-N`. Cosmetic; `--foreground` shows `[MainThread]`.
2. **1008 errors** — Windows fires `WTS_REMOTE_CONNECT` (3) + `WTS_CONSOLE_CONNECT` (1) **before** `WTS_SESSION_LOGON` (5). `SvcOtherEx` calls `start_session` for all three (to also handle fast-user-switch-back). During the connect-before-logon window no user token exists yet → `WTSQueryUserToken` → `1008 ERROR_NO_TOKEN`. `start_session` logs ERROR + returns; the later LOGON event then succeeds. Self-healing; the ERROR level is just misleading.

### Fix
- `orchestrator/supervisor.py: start_session` — in the `user_token_for_session` `except`, special-case `getattr(exc, "winerror", None) == 1008` (ERROR_NO_TOKEN): log at **INFO** ("session N has no interactive user yet (connect-before-logon); will start on logon") and return; keep `ERROR` for any other failure.
- `orchestrator/service.py: SvcDoRun` (polish) — `threading.current_thread().name = "svc-main"` before `run_core`, so service logs read `[svc-main]` instead of `[Dummy-1]`. (Settable on a `_DummyThread`; verify during implementation.)

### Verification
Harness still 25/25. On VM after redeploy: a fresh logon logs an INFO "no user token yet" for the connect events (no ERROR), then the normal spawn; service lines read `[svc-main]`.

### Risks
None functional — log-level/labels only. Keeping `start_session` on the connect events still gives fast-user-switch-back re-ensure (token exists then, so no 1008).

## Post-implementation fix #9 — uninstall leaves `C:\Program Files\DLP` behind  **[DONE — code + proven; redeploy to confirm on VM]**

### Symptom
After `uninstall.cmd` (and a rerun for the idempotency test), `rmtree` logs *"some files locked after 3 attempts; scheduled for delete-on-reboot"* — and the folder is **still present even after a reboot**.

### Root cause
`_rmtree_with_retry` (`installer.py:256`) is meant to delete the whole install tree, falling back to `MoveFileExW(MOVEFILE_DELAY_UNTIL_REBOOT)` for locked files. Three lock/limitation sources combined:
1. **Self-lock (dominant).** `uninstall.cmd` runs the **installed** `%ProgramFiles%\DLP\python\python.exe`. A live interpreter locks its own `python.exe` + every loaded `.dll`/`.pyd` under `…\python\`, so `rmtree` can't remove that subtree while the uninstaller runs → the entire `python\` tree (hundreds of files) is punted to reboot.
2. **explorer-pinned native DLLs.** `Payload.dll` (injected into every session's `explorer.exe`) and `DlpShellExt.dll` (shell-ext loaded by explorer) stay loaded → those files stay locked until explorer restarts.
3. **Reboot-delete most likely skipped by Fast Startup.** `MOVEFILE_DELAY_UNTIL_REBOOT` *does* delete files **and empty dirs** at restart (in order) per the MS docs — so a *true Restart* should have removed the tree. The likely reason it didn't: Windows **Fast Startup** (the default for "Shut down") hibernates the kernel session and does **not** run a full boot, so `PendingFileRenameOperations` isn't processed; only a real **Restart** is a full cold boot. (A VMware "power off → power on" behaves like Shut down, not Restart.) Combined with the self-lock bloating the scheduled set, the folder survived.

### Evidence (documented)
- **MoveFileEx / delete-on-reboot** — [MS Learn: MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw): with `MOVEFILE_DELAY_UNTIL_REBOOT` + `lpNewFileName=NULL` it "registers the file to be deleted when the system restarts"; "If lpExistingFileName refers to a directory, the system removes the directory at restart **only if the directory is empty**"; ops run "in the same order"; usable "only if the process is in the context of … administrators … or the LocalSystem account"; and "the file cannot exist on a remote share." Stored in `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\PendingFileRenameOperations`.
- **Fast Startup vs Restart** — [MS Learn: Fast startup](https://learn.microsoft.com/en-us/troubleshoot/windows-client/setup-upgrade-and-drivers/fast-startup-causes-system-hibernation-shutdown-fail) + widely documented: Fast Startup applies to **Shut down**, not **Restart**; Restart is a full cold boot. Pending reboot operations are reliably processed only on a true Restart.
- **Renaming an in-use image** — the standard "rename the loaded DLL/EXE aside, drop the new one, schedule the old for reboot-delete" update pattern is confirmed across sources (e.g. [Quora: rename in-use exe/DLL](https://www.quora.com/Is-it-possible-to-rename-an-executable-or-DLL-to-anything-on-Windows), microsoft.public.vc thread). Mechanism: the image loader opens with `FILE_SHARE_DELETE`, and rename needs only DELETE access — [Raymond Chen: renaming is multi-step](https://devblogs.microsoft.com/oldnewthing/20211022-00/?p=105822). A mapped image can be **renamed/moved** (same volume) but **not deleted** while loaded. *This is the one claim I'll also prove empirically before relying on it (see Verification).* 
- **explorer auto-restart is NOT guaranteed** — controlled by `HKLM\…\Winlogon\AutoRestartShell` (your point); `taskkill` only terminates. Hence the fix does **not** kill explorer.

### Fix
Eliminate the locks so the tree is removed synchronously, **without killing explorer** (explorer auto-restart via Winlogon `AutoRestartShell` is not guaranteed, and an uninstaller must never risk leaving the user shell-less):
- **`scripts/package-bundle.ps1` → uninstall.cmd**: prefer the **bundle/script-dir** python (`%~dp0python-embed\python.exe`, which lives *outside* the install tree), falling back to the installed python only if the bundle's is absent. Removes the self-lock — the uninstaller never runs from inside the directory it deletes.
- **`orchestrator/installer.py: _rmtree_with_retry`** (new strategy — "move locked files aside"): after the plain `shutil.rmtree` retries fail, walk the tree and delete everything deletable; for each **locked** file, `MoveFileExW(file, <pending>, MOVEFILE_REPLACE_EXISTING)` it into a same-volume pending dir (e.g. `<install_root.parent>\.DLP.pending-delete\<uuid>`), then `MoveFileExW(<pending file>, NULL, MOVEFILE_DELAY_UNTIL_REBOOT)`. A loaded image (EXE/DLL) can be *renamed/moved* on NTFS even while mapped (it just can't be *deleted*) — this is the self-updater pattern. With the locked files moved out, the now-empty install dirs are removed **immediately**; only the tiny moved-aside copies wait for reboot. If a move itself fails (older NTFS edge), fall back to scheduling that file in place via `_schedule_delete_on_reboot` (current behavior — strictly no worse).
- Keep `_schedule_delete_on_reboot` as the final fallback.

### Verification
- **Empirical proof — DONE (host).** (1) Loaded a real DLL via `ctypes.WinDLL`; `os.remove` → `PermissionError` winerror=5 (loaded images can't be deleted → confirms the self-lock + explorer-pin root cause); `os.replace` to a same-volume sibling → **SUCCESS** (move-aside foundation). (2) Drove the **actual** `_rmtree_with_retry` against a mock install tree with a *loaded* `Payload.dll` inside: tree `exists` went `True → False` (removed immediately) and the locked DLL landed in `.DLP.pending-delete\0000_Payload.dll`. Harness 25/25; `installer` imports clean; `package-bundle.ps1` parses. (The MoveFileEx reboot-scheduling of the aside copy is admin-gated — fine in the elevated uninstaller; the synchronous tree removal — the user's actual complaint — needs no admin and no reboot.)
- On VM: `uninstall.cmd` (admin) → `Test-Path "$env:ProgramFiles\DLP"` returns **False** with **no reboot and no explorer restart**. Rerun `uninstall.cmd` → idempotent, exit 0. Harness installer tests still pass (synthetic steps; real locks not exercised there).
- **Current leftover cleanup (manual, now):** delete the half-removed tree with `Remove-Item -Recurse -Force "$env:ProgramFiles\DLP"` after `Stop-Service DLPAgent`. If a native DLL is still pinned by `explorer.exe`, use **Restart** (Start ▸ Power ▸ Restart — *not* Shut down, so Fast Startup doesn't skip the pending deletes) and retry.

### Risks
- Moving a loaded image within the same volume is well-established (self-updating installers rely on it) but is verified at implementation time; the in-place reboot-schedule fallback covers any environment where it doesn't.
- Pending-delete folder lives on the same volume as `install_root` (so the move is a rename, not a cross-volume copy); cleaned at reboot.
- Running the uninstaller from the bundle on a UNC share works (Python imports from UNC), just slightly slower.

---

## Status — PHASE E COMPLETE ✅

E0–E7 + PF#1–#9 all implemented and verified. **VM end-to-end smoke PASSED** (2026-06-05): install via `install.cmd`; `Start-Service DLPAgent`; all 3 children stay up; clipboard/USB/browser BLOCK+ALLOW; USB hook + "Transfer to USB" menu + TransferAgent (PF#6); browser block popup on the user desktop (PF#7); admin + non-admin fast-user-switch sessions each get their own children + proxy; benign log noise gone (PF#8); `uninstall.cmd` removes the whole tree with no reboot/leftover (PF#9). Harness 25/25.

## Remaining steps

1. **Update `integration-plan2.md`** — mark Phase E ✅ COMPLETED (Goal-achieved + Outcomes + PF summary + Done-when), resolve open questions 7–10, de-stub the `session.py`/`service.py` snapshot lines. *(non-plan file — applied after ExitPlanMode)*
2. **Commit** — single bundled Phase E commit (message below).

## Phase E commit message (proposed)

```
Phase E: LocalSystem service + session-aware spawning + deployable bundle

Make the DLPAgent service do real DLP work: run as LocalSystem, spawn the
interceptors into the correct sessions, follow logon/logoff, deliver real
decisions. Add a self-contained bundle so a clean Win11 VM installs with no
Visual Studio / Developer PowerShell / dotnet.

Core (E0-E7):
- orchestrator/session.py: WTS session enumeration; WTSQueryUserToken ->
  DuplicateTokenEx -> CreateEnvironmentBlock -> CreateProcessAsUser
  (lpDesktop winsta0\default); per-session HKCU proxy via HKEY_USERS\<SID>;
  SID resolution; elevated linked-token (fallback B).
- orchestrator/supervisor.py: session-aware (session_id, child) table +
  start_session/stop_session; foreground keeps Phase C behavior (new
  machinery gated on service_mode).
- orchestrator/service.py: real SvcDoRun via shared run_core;
  GetAcceptedControls |= SERVICE_ACCEPT_SESSIONCHANGE; SvcOtherEx routes
  logon/connect -> start_session, logoff -> stop_session.
- orchestrator/__main__.py: run_core(config_path, stop_event, *, foreground)
  shared by --foreground (Ctrl+C) and --service (SvcStop).
- Controller: enable SeDebugPrivilege (Privileges.cs) for cross-session
  injection from Session 0 (spike-confirmed on Win11 26200; Option A).
- config.yaml: peripheral_storage.controller_in_user_session toggle.
- scripts/harness/test_session.py: session-bridge driver tests (harness 25/25).

Deployable bundle:
- scripts/prepare-python-embed.ps1: bundle analyzer deps; copy pre-compiled
  pyahocorasick from .venv (the embed can't compile C extensions).
- installer.build_bundle_config + scripts/package-bundle.ps1: lean dist\DLP\
  + dist\DLP.zip with VM-ready config and install.cmd/uninstall.cmd.

Post-implementation fixes (clean-VM deployment):
- mitmdump launched via sys.executable -c shim (pip launcher hardcoded the
  host python path) + --set confdir for the installed CA.
- Payload.dll / DlpShellExt.dll -> static CRT (/MT(d); detours
  x64-windows-static) so they load with no VC++ debug runtime on the VM.
- browser block popup via WTSSendMessageW (Session 0 -> active console
  desktop) instead of MessageBoxW.
- uninstall removes the install tree synchronously: uninstall.cmd runs the
  bundle python (no self-lock); _rmtree_with_retry moves locked DLLs aside
  (same-volume rename) instead of relying on reboot or killing explorer.
- session-change log noise: 1008 connect-before-logon -> INFO; service thread
  named svc-main; installer completion message de-staled.

VM prerequisite: .NET 10 Desktop Runtime (apps are framework-dependent .NET).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
