# DLP Endpoint Agent — Build, Test & Deploy Guide

An endpoint Data-Loss-Prevention agent for **Windows 11 x64**. It intercepts outbound user data on three channels and routes it to a Python content analyzer that ALLOWs / BLOCKs based on policy:

- **peripheral_storage** — blocks file copies to removable drives (DLL-injected `NtCreateFile` hook) and forces transfers through a scanned "Transfer to USB (DLP Protected)" agent.
- **browser** — intercepts uploads to Google Drive / Gmail / Zalo via a local mitmproxy.
- **clipboard** — intercepts text copied to the clipboard (and keeps Windows clipboard history disabled).

A LocalSystem Windows service (`DLPAgent`) supervises the interceptors across user sessions and orchestrates them to the analyzer.

> **Two audiences:**
> - **§A Dev environment** — full toolchain installed; build / automated-test / install / manual-test / uninstall, all from source.
> - **§B Test environment** — a clean Windows 11 VM with **no** dev tools; install / test / uninstall a prebuilt bundle.

> **Pre-test markers.** Steps verified on the author's dev box are tagged **✅ PRE-TESTED**. Steps confirmed by a full manual run — build → bundle → install on the clean Windows 11 VM → end-to-end decision tests → uninstall (2026-06-24) — are tagged **✅ MANUALLY TESTED**. The only step still requiring a Visual Studio Developer PowerShell to re-verify from scratch is tagged **⚠️ NOT PRE-TESTED — verify on first run**.

---

## Command-form convention (read this first)

Throughout, `python` / `pytest` assume you have **activated the repo virtual environment**:

```powershell
# from the repo root, in a normal PowerShell:
.\.venv\Scripts\Activate.ps1
```

If `python` is not on your PATH (or you skip activation), fall back to the **absolute interpreter path** — every Python command below works in this form too:

```powershell
# author's machine used this exact path; adjust <RepoRoot> to your clone location:
& "<RepoRoot>\.venv\Scripts\python.exe" -m pytest scripts\harness -q
# author's RepoRoot was: D:\Code\GithubPublishEndpointDLP
```

`<RepoRoot>` below means the folder you cloned into (the one containing `config.yaml`).

---

# §A — Dev Environment

## A.1 Prerequisites

Install these once. Everything the build/test/run/install needs is listed here.

| # | Requirement | Notes |
|---|---|---|
| 1 | **Windows 11 x64** | The agent and tests are Windows-only (named pipes, Win32, pywin32). |
| 2 | **Visual Studio 2026 Community** with workloads:<br>• **.NET desktop development** (brings the **.NET 10 SDK** for `dotnet build`)<br>• **Desktop development with C++** (MSVC v143+, **Windows 11 SDK**, and **vcpkg**) | Provides the **VS 2026 Developer PowerShell v18.5.2** used for all C#/C++ builds. The author's MSBuild is at `C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe` (also referenced by the build script). |
| 3 | **C++20** toolchain | Comes with the C++ workload; the native projects target C++20. |
| 4 | **Python 3.13 (x64)** on PATH | For the dev `.venv`, the analyzer, the orchestrator, and the test harness. The bundled runtime is a separate Python 3.13 *embeddable* produced by a script (A.3). |
| 5 | **vcpkg + internet (first build only)** | The native `Payload.dll` depends on **Microsoft Detours** via vcpkg **manifest mode** (`interceptors/peripheral_storage/Payload/vcpkg.json`). `vcpkg_installed/` is git-ignored, so the **first** `msbuild` restores `detours` (triplet `x64-windows-static`) from the internet. VS 2026's bundled vcpkg satisfies this. |
| 6 | **Git** | To clone the source. |
| 7 | **Admin rights** | Needed for the dev-box **install/uninstall** (A.7/A.10), the admin CLI (`dlp-ctl status`/`reload`), and the manual test (HKLM, LocalMachine cert store, `sc.exe`). Building and the automated tests do **not** need admin (except the 3 admin-pipe tests, which skip otherwise). |
| 8 | **.NET 10 Desktop Runtime** | Already provided by the VS .NET workload on the dev box. (The clean VM needs it installed separately — see §B.) |

A fresh GitHub clone contains **only source** — `.venv/`, `python-embed/`, built `bin`/`obj`, `vcpkg_installed/`, and `dist/` are all git-ignored and produced by the steps below.

## A.2 One-time Python environment setup

From the repo root. Create the dev virtual environment and install dependencies. `pyahocorasick` **compiles from source** (no cp313 wheel), so install it from a shell that has the MSVC compiler on PATH — i.e. a **VS 2026 Developer PowerShell** (or "x64 Native Tools" prompt). The other deps are wheels and install from any shell.

```powershell
# 1) create the venv (normal PowerShell, from <RepoRoot>):
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2) install deps — run THIS step from a VS 2026 Developer PowerShell so
#    pyahocorasick can compile (the rest are prebuilt wheels):
.\.venv\Scripts\Activate.ps1                 # activate again inside the dev shell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r analyzer\requirements.txt   # compiles pyahocorasick
python -m pip install pytest                  # test runner (not in requirements.txt)
```

Verify the analyzer deps import:

```powershell
python -c "import ahocorasick, re2, fitz, win32service, mitmproxy, yaml, watchdog; print('deps OK')"
```

> ✅ **MANUALLY TESTED** — the venv setup underpinned the full build → bundle → VM-install pass (2026-06-24); the dep-import self-check above printed `deps OK`.

**`requirements.txt`** (orchestrator/runtime): `mitmproxy`, `pywin32`, `pyyaml`, `watchdog`.
**`analyzer/requirements.txt`** (analyzer): `google-re2`, `pyyaml`, `pyahocorasick`, `python-docx`, `openpyxl`, `python-pptx`, `odfpy`, `PyMuPDF`, `pymupdf-layout`.

## A.3 Build — bundled Python embeddable (`python-embed\`)

Produces `<RepoRoot>\python-embed\` (a self-contained Python 3.13 with all deps + the pre-compiled `pyahocorasick` copied from your `.venv`). The installer ships this as `C:\Program Files\DLP\python\`, so the VM needs no Python.

```powershell
# normal PowerShell, from <RepoRoot> (downloads ~50–100 MB, grows to ~200–400 MB):
.\scripts\prepare-python-embed.ps1
```

Requires that A.2 succeeded (it copies `ahocorasick*.pyd` + `pyahocorasick-*.dist-info` out of `.venv\Lib\site-packages`). Needs internet (downloads the embeddable + get-pip, then `pip install`s the deps from PyPI).

> **Download resilience / "An existing connection was forcibly closed by the remote host".** python.org's CDN sometimes resets the connection mid-download. The script downloads via `curl.exe --retry-all-errors` (Windows built-in) with an `Invoke-WebRequest` fallback, and **reuses an already-downloaded file** at `%TEMP%\python-<ver>-embed-amd64.zip` (and `%TEMP%\get-pip.py`). So if a download keeps failing or is very slow, fetch the URL in a browser to that exact `%TEMP%` path and re-run — the script skips the download. (Observed on the dev box: a degraded link made the ~12 MB embed take several minutes even via curl; be patient or retry on a better network.)

> ✅ **MANUALLY TESTED** — `prepare-python-embed.ps1` ran end-to-end (download + pip bootstrap + dep install + `embed OK` self-check); the resulting embed shipped in the bundle that installed and ran on the clean VM (2026-06-24).

## A.4 Build — C# + C++ interceptor artifacts

Builds the 3 .NET 10 apps (`dotnet build`) and 2 native DLLs (`msbuild`). The **first** run restores `detours` via vcpkg (slower once; needs internet).

```powershell
# VS 2026 Developer PowerShell, from <RepoRoot>:
.\scripts\prepare-install-payload.ps1
```

What it builds (and where the installer expects them — `config.yaml` `paths:` defaults, **Debug**):

| Project | Tool | Output |
|---|---|---|
| `src\ClipboardInterceptor\ClipboardInterceptor.csproj` | `dotnet build` | `…\bin\Debug\net10.0-windows\ClipboardInterceptor.exe` |
| `interceptors\peripheral_storage\Controller\Controller.csproj` | `dotnet build` | `…\bin\Debug\net10.0-windows\win-x64\UsbDlpController.exe` |
| `interceptors\peripheral_storage\TransferAgent\DlpTransferAgent.csproj` | `dotnet build` | `…\bin\Debug\net10.0-windows\win-x64\DlpTransferAgent.exe` |
| `interceptors\peripheral_storage\Payload\Payload.vcxproj` | `msbuild` (x64) | `…\Payload\x64\Debug\Payload.dll` |
| `interceptors\peripheral_storage\ShellExtension\DlpShellExt.vcxproj` | `msbuild` (x64, `/p:SolutionDir=…\interceptors\peripheral_storage\`) | `…\out\ShellExtension\Debug\DlpShellExt.dll` |

The script verifies every artifact exists and fails loudly if one is missing. Use `-Configuration Release` to build Release, **but** `config.yaml`'s default paths point at `Debug`, so for install you must either build Debug or edit those paths. Default = **Debug**.

> ⚠️ **NOT PRE-TESTED** — requires the Developer PowerShell toolchain (not available in this session). If MSBuild is at a different path, edit `$MSBuild` at the top of the script, or pass the correct path.
>
> Direct fallback if the script can't be used (Developer PowerShell, from `<RepoRoot>`):
> ```powershell
> dotnet build src\ClipboardInterceptor\ClipboardInterceptor.csproj -c Debug
> dotnet build interceptors\peripheral_storage\Controller\Controller.csproj -c Debug
> dotnet build interceptors\peripheral_storage\TransferAgent\DlpTransferAgent.csproj -c Debug
> & "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe" interceptors\peripheral_storage\Payload\Payload.vcxproj /p:Configuration=Debug /p:Platform=x64
> & "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe" interceptors\peripheral_storage\ShellExtension\DlpShellExt.vcxproj /p:Configuration=Debug /p:Platform=x64 "/p:SolutionDir=<RepoRoot>\interceptors\peripheral_storage\"
> ```

## A.5 Automated tests

Two suites: the **Python** orchestrator/analyzer harness (pytest) and the **C#** client-library unit tests (xUnit). Run both.

### Python harness (pytest)

Spawns isolated orchestrator subprocesses and exercises pipe concurrency, policy hot-reload, **config hot-reload** (both that every hot-reloadable field applies live and that restart-only fields stay inert), dispatcher timeout/fail-closed, clipboard supersession, the supervisor, the installer driver, the admin-pipe + events log, and the bounded drain.

```powershell
# normal PowerShell, .venv activated, from <RepoRoot>:
python -m pytest scripts\harness -q
# verbose (shows each test + which 3 skip):
python -m pytest scripts\harness -v
# absolute form:
& "<RepoRoot>\.venv\Scripts\python.exe" -m pytest scripts\harness -q
```

**Expected: `222 passed, 3 skipped`.** The 3 skips are the **admin-pipe** tests in `test_admin.py` — they require an **elevated** process (the admin-pipe DACL is Administrators-only) and correctly skip under a normal prompt. To run them too, launch PowerShell **as Administrator** and re-run; they should pass.

- **Policies used by the automated tests:** `scripts\harness\fixture_policies\permissive.yaml` (default — allows everything) and `visa_block.yaml` (blocks a Visa-format number with no context). You normally do **not** edit these.
- **Config used:** auto-generated per test (unique pipe names) under `tmp\harness\<uuid>\config.yaml`.
- **Logs:** isolated per test under `tmp\harness\<uuid>\DLP\logs\` (the harness redirects `%PROGRAMDATA%` so tests never touch the real log dir). The whole `tmp\harness\` tree is cleaned on teardown.

> ✅ **PRE-TESTED** — `222 passed, 3 skipped` in ~59 s on the dev box (non-elevated, so the 3 admin-pipe tests skipped as expected). A trailing `cleanup_numbered_dir … PermissionError` line from pytest's own temp-symlink cleanup is a benign Windows quirk printed *after* the result and does not affect the outcome.

### C# unit tests (xUnit)

The shared C# client library has an xUnit v3 test project, `src\AgentCore.Tests\AgentCore.Tests.csproj` (`net10.0-windows`), covering:
- `PipeAgentCoreTests.cs` — the `PipeAgentCore` named-pipe client (incl. fail-closed on pipe error/timeout).
- `ConfigLocatorTests.cs` — `DlpShared.ConfigLocator` discovery (`DLP_CONFIG_PATH` env var → walk-up with the `data_pipe:` sentinel check).
- `PipeNameHelperTests.cs` — `DlpShared.PipeNameHelper` name conversion.
- `ClipboardSelfWriteTests.cs` — the clipboard self-write loop guard + the English block text (`[DLP] Blocked: <reason>` / `[DLP] Content blocked`).

```powershell
# any shell with the .NET 10 SDK on PATH (e.g. a VS 2026 Developer PowerShell), from <RepoRoot>:
dotnet test src\AgentCore.Tests\AgentCore.Tests.csproj
```

First run restores the xUnit / Test.Sdk NuGet packages (needs internet once). No admin required. These are independent of the Python suite — they don't touch pipes or the analyzer.

> ✅ **PRE-TESTED** — `17 passed, 0 failed` in ~2 s on the dev box (`dotnet` was on PATH at `C:\Program Files\dotnet\dotnet.exe`, so a normal PowerShell worked too). `CA2022` build warnings in `PipeAgentCoreTests.cs` are benign (code-analysis hints, not test failures).

> **Note on `--foreground`.** The automated harness uses `python -m orchestrator --foreground` internally (with `DLP_SUPERVISOR_DISABLED`), but **foreground is not a manual-test method**: in a normal session the Controller can't inject the USB hook (it needs admin + `SeDebugPrivilege`), and the browser channel does nothing until the mitmproxy CA is trusted and the proxy is set. Both only happen during **install**, so the dev-box manual test is done via a real install (A.7), exactly like the VM.

## A.6 Build the deployable bundle (for the VM)

Assembles a lean self-contained `dist\DLP\` + `dist\DLP.zip` (embed + built artifacts + browser addon + analyzer + a VM-ready `config.yaml` + `install.cmd`/`uninstall.cmd`/`dlp-ctl.cmd`/README).

```powershell
# normal PowerShell, from <RepoRoot> (after A.3 + A.4):
.\scripts\package-bundle.ps1
# if your session execution policy blocks scripts:
powershell -ExecutionPolicy Bypass -File scripts\package-bundle.ps1
```

> ✅ **MANUALLY TESTED** — `package-bundle.ps1` assembled `dist\DLP\` + `DLP.zip`, which installed and ran end-to-end on the clean VM (2026-06-24).

## A.7 Install on the dev box (the dev-box manual test)

On the dev box you manual-test the agent the way it runs in production — by installing it. This is the **primary** dev-box manual test (foreground is not a substitute — see the note in A.5). It exercises the **service**, **USB hook**, **shell extension**, and **browser HTTPS interception** end-to-end. Requires A.3 + A.4 done.

```powershell
# ELEVATED PowerShell, .venv activated, from <RepoRoot>:
python -m orchestrator --install --config config.yaml
# absolute form:
& "<RepoRoot>\.venv\Scripts\python.exe" -m orchestrator --install --config config.yaml
```

This installs into `C:\Program Files\DLP\`, registers the `DLPAgent` service (**start=auto**), **starts it immediately**, installs the mitmproxy CA into LocalMachine\Root, sets the HKCU proxy, registers the shell extension (HKLM), and adds `C:\Program Files\DLP` to the machine **PATH** + drops `dlp-ctl.cmd` there.

Verify:

```powershell
Get-Service DLPAgent          # expect Running
# open a NEW elevated shell so PATH refreshes, then:
dlp-ctl status
```

> ✅ **MANUALLY TESTED** — the elevated install registered + started `DLPAgent` and `dlp-ctl status` responded (2026-06-24); this is the same install flow as the VM (§B).

## A.8 Manual test (the installed agent)

### Admin CLI (`dlp-ctl`)

After install, `dlp-ctl` is on the machine PATH — open a **NEW** elevated shell so it's picked up. `status` / `reload` use the Administrators-only admin-pipe (elevated); `tail` just reads the log (any shell):

```powershell
dlp-ctl status                 # uptime, in-flight counts, last reloads, child table
dlp-ctl reload                 # force re-apply config + policies -> "reloaded: config, policies"
dlp-ctl tail --follow          # stream events.jsonl
dlp-ctl tail --log -n 80       # last 80 lines of dlp-agent.log
# if PATH hasn't refreshed yet, from C:\Program Files\DLP:
.\dlp-ctl.cmd status
```

> ✅ **MANUALLY TESTED** — `dlp-ctl status`/`reload`/`tail` over the installed PATH wrapper, including the live elevated round-trip and a `reload` that applied edited config + policies, passed on the clean VM (2026-06-24).

#### App Control (WDAC) — `dlp-ctl appcontrol` (Phase AC-4)

The standalone operator loop for the App Control channel. `allow`/`deny`/`build`/`apply` are **offline** local-file operations (run elevated); `status`/`disable` talk to the running agent. The agent's inbox watcher deploys whatever `apply` drops — no hand-built pushes, no central server.

```powershell
dlp-ctl appcontrol allow "C:\Program Files\7-Zip"      # add Allow targets (files/folders)
dlp-ctl appcontrol deny  "C:\…\OneDrive.exe"            # add Deny targets
dlp-ctl appcontrol allow "C:\…\old" --remove           # drop entries from a list
dlp-ctl appcontrol build                               # compile lists -> staging\build\ (auto-bumps VersionEx)
dlp-ctl appcontrol apply                               # move the staged push into the inbox (go live)
dlp-ctl appcontrol status                              # lists + staged build + deployed policy/blocks
dlp-ctl appcontrol disable                             # remove the deployed policy (via the agent)
dlp-ctl appcontrol disable --force-local               # emergency removal driving citool directly (agent-down escape)
```

- Lists live at `C:\ProgramData\DLP\appcontrol\{allow,deny}-list.txt`; folders are re-scanned for executables at every `build`. Each target gets a WDAC rule on its **InternalName**; a file with no usable PE version-info falls back to a **Hash** rule automatically. Self-protect rules (`<install_root>\*` + `C:\Program Files\dotnet\*`) are always merged so the agent stays runnable under its own policy.
- `build` compiles with `ConvertFrom-CIPolicy`, so the endpoint needs the **ConfigCI** module. The installer's `enable_configci` step DISM-enables it automatically at install time (fail-closed — a clean install always has a working on-endpoint `build`), so no manual step is needed. To opt out, set `app_control.enabled: false` in config.yaml — that skips both the channel and the ConfigCI enable; if you later want on-endpoint `build` while disabled, enable ConfigCI once with:
  `Get-ChildItem $Env:SystemRoot\servicing\Packages\*ConfigCI*.mum | % { dism /online /norestart /add-package:"$($_.FullName)" }`
- **Uninstall** removes any deployed App Control policy (`citool --remove-policy`, no reboot) and strips the whole `C:\ProgramData\DLP\appcontrol\` tree (lists + pushes) plus the status record, leaving the box as it was before install. The uninstaller runs from the **installed** python (`C:\Program Files\DLP\python`), which the self-protect policy allows, so uninstall works **even while a policy is enforced** — the bundle's embed python would be blocked. The installer also drops `C:\Program Files\DLP\uninstall.cmd` so you can uninstall after the deploy bundle is gone. To **re-install over a still-enforced policy**, disable it first (`dlp-ctl appcontrol disable`), then run `install.cmd` — a fresh install uses the bundle python, which a live policy would otherwise block.

> ✅ **MANUALLY TESTED** — list management, a real `ConvertFrom-CIPolicy` build + `apply`, and the live deploy/block/disable round-trip on the installed service were verified on the clean VM (2026-06-24).

**Where to change the INPUT (what gets blocked):** edit the **installed** policy file, then reload —

- Installed policies: `C:\Program Files\DLP\analyzer\policies.yaml`
- Installed config: `C:\Program Files\DLP\config.yaml`
- After editing either: `dlp-ctl reload` (elevated) — or just save; the file-watcher auto-applies.

**What the default policies block** (`analyzer/policies.yaml`) — note all require a **context word nearby**, so a bare number is NOT blocked:

| Policy | Triggers on | Needs a context word within | Action |
|---|---|---|---|
| `block_visa_all_channels_with_context` | Visa-format card, e.g. `4111 1111 1111 1111` | 120 chars of: `credit card`, `thẻ tín dụng`, `card number`, `số thẻ`, `thẻ`, `visa` | **BLOCK** |
| `block_cccd_all_channels_with_context` | Vietnamese CCCD 12-digit `0xx…`, e.g. `012301234567` | 200 chars of: `CCCD`, `căn cước`, `CMND`, `số định danh`, … | **BLOCK** |
| `log_phone_numbers_browser` | VN phone `09xxxxxxxx` / `+84…` | 100 chars of: `số điện thoại`, `sđt`, `phone`, … | **ALLOW (logged)** |

Make a test file/text that contains **both** a matching number **and** a context word (e.g. a `.csv`/`.txt`/`.docx` with `credit card: 4111 1111 1111 1111`). A clean file (no PII, or PII without context) should **ALLOW**.

**Manual checks:**
1. **USB block** — copy any file to a removable drive via Explorer → blocked; right-click a file → **"Transfer to USB (DLP Protected)"** → the TransferAgent scans it → ALLOW for clean, BLOCK for a CCCD/Visa-with-context file. The results **Note** column now shows a human reason — the policy's `user_message` on a policy block (e.g. *"Vietnamese Citizen ID (CCCD/CMND) detected"*), or a friendly failure reason (e.g. *"File type is not supported"* for an unsupported type) — instead of the file hash. (All end-user notifications are in **English**.)
2. **Browser** — upload the test file via Google Drive / Gmail → BLOCK shows a popup on your desktop carrying the reason **and** an instruction to reload the page and stop the upload (a blocked upload surfaces in Drive as a "network error" and otherwise retries); clean upload proceeds.
3. **Clipboard** — copy text containing a card+context → blocked, and the clipboard is replaced with `[DLP] Blocked: <reason>` (English). Re-copy normal text → restored/allowed (the replacement text is excluded from re-analysis, so it cannot loop). **Large text:** the interceptor now sends the *entire* copied text (there is **no** client-side byte cap) — copy the whole contents of a big text file (e.g. a ~7 MB `.txt`) and it is analyzed in full and blocked/allowed on its content. Whether the text is scanned is governed by `analyzer.max_extracted_chars`: text **longer** than that is refused without scanning (`reason=text_cap` in `dlp-agent.log`) and follows `clipboard.failure_mode`. `max_extracted_chars` is **hot-reloadable** (`dlp-ctl reload` / save). (The data pipe still bounds reassembly memory at a ceiling derived from `max_extracted_chars`, so a pathologically huge copy is dropped and fails per `failure_mode`.)
4. **Audit** — confirm a line per decision in `events.jsonl` (a BLOCK line carries a `reason` category — see §A.9) and a BLOCK/ALLOW line in `dlp-agent.log`.
5. **Failure mode** — every channel has a `failure_mode` (`fail_closed` → BLOCK, the default; `fail_open` → ALLOW) that decides the verdict when analysis can't complete (oversize file, **text over `max_extracted_chars`**, timeout, analysis error, the **unsupported file type**, or — for the clients — the orchestrator pipe being unreachable). Quick demo via the clipboard text-cap path: set `analyzer.max_extracted_chars: 100` in `config.yaml`, `dlp-ctl reload`, then copy any longer text → **BLOCK** (`reason=text_cap` in `dlp-agent.log`); now add `clipboard.failure_mode: fail_open`, `dlp-ctl reload`, copy again → **ALLOW**. Restore the values + reload when done. **This now exercises a true SERVER-side hot-reload:** `failure_mode`, `limits.max_file_bytes`, `analyzer.max_extracted_chars`, `analyzer.supported_extensions`, and `service.analysis_timeout_seconds` are applied live by the orchestrator on `dlp-ctl reload`/save (previously they were frozen at startup). (The orchestrator-side failure modes and the config hot-reload are covered automatically by `scripts\harness\test_failure_mode.py`, `test_config_apply_hot_reload.py`, and `test_config_hot_reload_e2e.py`.) **Unsupported formats:** only the extensions in `analyzer.supported_extensions` (the 8 tested formats — `docx/odt/ods/xlsx/csv/txt/md/pdf` — plus textual `tsv/json/yaml/yml/log`) are scanned; any other type with an **explicit** extension (e.g. `.pptx`, `.exe`, an image) is refused with `reason=unsupported_format` and follows `failure_mode`. A file with **no** extension is **not** refused — it is analyzed as plaintext (some upload paths, notably **Gmail**, strip the extension and deliver every file as `upload`, so blocking on a missing extension would block legitimate `.txt`/`.csv`/`.md` uploads). `supported_extensions` is now **hot-reloadable** (no restart needed). **Behavior:** the **browser** channel defaults to `fail_closed`, so uploads are **blocked** if the orchestrator is unreachable.

> ✅ **MANUALLY TESTED** — all five checks passed on the clean VM (2026-06-24): USB block + Transfer Agent (English Note), browser upload popup (English reason + reload/stop guidance), clipboard block with the English `[DLP] Blocked: <reason>` text **and** the large-copy + `analyzer.max_extracted_chars` `text_cap` reload demo, the `events.jsonl`/`dlp-agent.log` audit lines (incl. `config hot-reload applied: …`), and the `failure_mode` BLOCK↔ALLOW flip on `dlp-ctl reload`.

## A.9 Logs & locations (where to check OUTPUT)

| Path | Contents |
|---|---|
| `C:\ProgramData\DLP\logs\dlp-agent.log` | Main log: startup, BLOCK/ALLOW decision lines (with `elapsed_ms` + `policy_id(action)×N`), reloads, errors. |
| `C:\ProgramData\DLP\logs\events.jsonl` | **Structured audit** — one JSON line per decision: `ts, req_id, channel, kind, decision, violations:[{policy_id,count}], elapsed_ms, superseded, name, url` (URL query-stripped), and `reason` on a BLOCK — a stable category token (`policy_violation` / `oversize` / `text_cap` / `unsupported_format` / `timeout` / `analysis_error` / `malformed`) so a block's cause is unambiguous (cf. ECS `event.reason`). |
| `C:\ProgramData\DLP\logs\supervisor-{mitmdump,clipboard,controller}.log` | Per-child process output. |
| `C:\ProgramData\DLP\state\` | `install_manifest.json`, `installed_ca.txt`, `proxy_backup*.json`. |
| `C:\ProgramData\DLP\mitmproxy\` | Generated mitmproxy CA (trusted into LocalMachine\Root by install). |

The installed service logs to `C:\ProgramData\DLP\logs\`. (The automated tests are the exception — they redirect to `tmp\harness\<uuid>\DLP\logs\`.)

> ✅ **MANUALLY TESTED** — these paths and contents were confirmed on the clean VM (2026-06-24): the `dlp-agent.log` decision lines + the `config hot-reload applied: …` line, and the `events.jsonl` `reason` tokens (including `text_cap` for an over-cap clipboard copy).

## A.10 Uninstall (dev box)

```powershell
# ELEVATED PowerShell, .venv activated, from <RepoRoot> (use the REPO .venv python,
# NOT the installed one, to avoid the uninstaller locking the tree it deletes):
python -m orchestrator --uninstall --config config.yaml
# absolute form:
& "<RepoRoot>\.venv\Scripts\python.exe" -m orchestrator --uninstall --config config.yaml
```

Reverses everything (service stop+delete, CA removal, proxy restore, shell-ext unregister, PATH entry + wrapper removal, tree delete). Idempotent — safe to re-run. Verify:

```powershell
Get-Service DLPAgent 2>$null                                  # should be gone
Test-Path "$env:ProgramFiles\DLP"                             # False
[Environment]::GetEnvironmentVariable('Path','Machine')       # no C:\Program Files\DLP entry
```

> ✅ **MANUALLY TESTED** — uninstall reversed the service stop+delete, CA removal, proxy restore, shell-ext unregister, PATH entry + wrapper removal, and tree delete on the VM (2026-06-24); the verify checks above all came back clean.

---

# §B — Test Environment (clean Windows 11 VM, no dev tools)

The VM only runs the **prebuilt bundle** from A.6. No Python, Visual Studio, or dev tools required. (The author's reference VM had ~32 GB free; the bundle is a few hundred MB.)

## B.1 Prerequisites

| # | Requirement | Notes |
|---|---|---|
| 1 | **Windows 11 x64** VM | Clean install is fine. |
| 2 | **.NET 10 Desktop Runtime (x64)** — install once | The interceptor apps are framework-dependent .NET 10. Download "**Windows Desktop Runtime 10.0.x x64**" from <https://dotnet.microsoft.com/download/dotnet/10.0> and run (elevated):<br>`windowsdesktop-runtime-10.0.x-win-x64.exe /install /quiet /norestart`<br>Verify (if the dotnet CLI is present): `dotnet --list-runtimes` → expect a `Microsoft.WindowsDesktop.App 10.x` line. |
| 3 | **Administrator account** | Install/uninstall write HKLM, the LocalMachine cert store, the service, and machine PATH. |
| 4 | The **bundle** (`dist\DLP\` folder or `dist\DLP.zip`) from A.6 | Copy it onto the VM (shared folder, zip, etc.). No other source needed. |

Everything else (the Python runtime, all deps, the native DLLs with static CRT) is inside the bundle — see §A for how it's produced.

## B.2 Install

1. Copy the bundle folder (or unzip `DLP.zip`) anywhere on the VM.
2. **Right-click `install.cmd` → "Run as administrator"** (it runs the bundled embed Python — no system Python needed).
3. The installer registers `DLPAgent` (**start=auto**), **starts it now**, installs the CA + proxy + shell extension, and adds the install dir to PATH + drops `dlp-ctl.cmd`.
4. Verify it's running:
   ```powershell
   Get-Service DLPAgent            # expect Running
   Get-Content "$env:ProgramData\DLP\logs\dlp-agent.log" -Tail 40
   ```
   If Stopped: `Start-Service DLPAgent` (note: bare `sc start` in PowerShell is an alias for `Set-Content` — use `Start-Service` or `sc.exe start DLPAgent`).

> ✅ **MANUALLY TESTED** — on the clean VM (2026-06-24) `install.cmd` (elevated, bundled embed Python) registered + **auto-started** `DLPAgent`, installed the CA + proxy + shell extension, and put `dlp-ctl` on the machine PATH.

## B.3 Test on the VM

**Admin CLI** — open a **NEW** (elevated) PowerShell so the PATH change is picked up, then:

```powershell
dlp-ctl status            # uptime, in-flight, child table (Running children per session)
dlp-ctl tail --follow     # live events.jsonl
# if PATH hasn't refreshed yet, from C:\Program Files\DLP:
.\dlp-ctl.cmd status
```

**Decision tests** — same as A.8, using the **installed** files:
- Input policies: `C:\Program Files\DLP\analyzer\policies.yaml` (edit + `dlp-ctl reload` to change what's blocked).
- Output: `C:\ProgramData\DLP\logs\dlp-agent.log` and `events.jsonl` (locations identical to §A.9).

Run through: USB copy block + "Transfer to USB" agent; Google-Drive/Gmail upload block (popup on the user desktop) + clean upload; clipboard text block (English `[DLP] Blocked: <reason>`) — **including a large ~7 MB copy** (sent in full and analyzed, not size-rejected) and the `failure_mode` + server-side hot-reload demo from §A.8 (steps 3 & 5, now via `analyzer.max_extracted_chars` → `reason=text_cap`); confirm `events.jsonl` lines (with `{policy_id,count}` violations and a query-stripped URL). Test across an **admin** and a **standard (non-admin)** user session (fast-user-switch) — each gets its own interceptors; the non-admin session's `dlp-ctl status` should be **denied** (admin-pipe is Administrators-only).

Reboot the VM → the service should **auto-start** (no manual start needed).

> ✅ **MANUALLY TESTED** — the full VM end-to-end run passed (2026-06-24): USB block + Transfer Agent, Google-Drive/Gmail upload block + clean upload, clipboard block **including the large ~7 MB copy and the `analyzer.max_extracted_chars` `text_cap` + `failure_mode` reload demo**, the `events.jsonl` lines, the admin vs non-admin session split (non-admin `dlp-ctl status` denied), and the reboot **auto-start**.

## B.4 Uninstall (VM)

**Right-click `uninstall.cmd` → "Run as administrator"** (it prefers the bundle's own Python so it never locks the install tree). Reverses everything and is safe to re-run. Verify as in A.10. If a native DLL is still pinned by `explorer.exe`, use **Start ▸ Power ▸ Restart** (a true restart, not Shut down — Fast Startup skips pending deletes) and re-check.

> ✅ **MANUALLY TESTED** — `uninstall.cmd` (elevated) reversed everything on the VM and the verify checks came back clean (2026-06-24).

---

# §C — Connect the agent to the Management Console (server)

Everything above runs the agent **standalone** (local `policies.yaml`, local `dlp-ctl`). This section connects an installed agent to the **Management Console** server so it appears in the web UI, sends heartbeats/logs, and receives server-authored policies. It is **optional** — `server.enabled: false` (the default) keeps the agent fully standalone and changes nothing above.

> **Topology used here:** the **server** runs in Docker on the **dev box**; the **agent** runs on the clean **VM**, reaching the dev box over **VMware NAT**. The server lives only in Docker (reversible), so it never touches the dev box's WDAC/service state.

> **Phase note (current capability):** connecting makes the agent go **ACTIVE** in the console, advances `last_seen`, ships log tails, and **delivers** assigned policies to the agent's `analyzer/policies.yaml`.
>
> ✅ **Phase 1 done + VM-verified (2026-06-27).** Server-authored policies are now **score-based and enforce** on the VM (the UI exposes `score_base`/`score_context_boost` + an action ladder + a block reason), and the agent reports every **notable** decision back to the console's **Violation Logs** as one event with its matched policies — policy blocks, monitored `allow_log` hits, and failure-mode outcomes (`fail_closed` BLOCK / `fail_open` ALLOW), all queryable by `reason`. **When authoring a policy, enter regex patterns with SINGLE backslashes** (e.g. `\b\d{12}\b`) — the UI warns on a double-backslash paste. See `agent-server-integration-plan.md` Phase 1 + `phase1-policies-violations-plan.md`. Connecting does **not** weaken standalone enforcement.

## C.1 Deploy the server (dev box, Docker)

Requires **Docker Desktop** running. From the repo:

```powershell
cd src\management_console
docker compose up -d --build      # builds backend+frontend images, starts db/backend:8000/frontend:80
docker compose ps                 # expect 3 containers Up (ports 8000, 5432, 80)
```

`server\.env` already ships with working PoC values (admin `admin` / `admin123`, `SECRET_KEY`, `CORS_ORIGINS=["*"]`); compose overrides `DATABASE_URL` to the `db` container, so no edit is needed to start. On startup the backend runs `alembic upgrade head` and seeds the admin + settings. Verify:

```powershell
curl.exe -s -o NUL -w "api=%{http_code}`n" http://localhost:8000/openapi.json   # 200 (also browse /docs)
curl.exe -s -o NUL -w "ui=%{http_code}`n"  http://localhost/                     # 200
```

Open **http://localhost/** and log in `admin` / `admin123`.

> ✅ **MANUALLY TESTED** (2026-06-26) — `docker compose up -d --build` built both images (`node:26-alpine` + `python:3.13-slim`), ran the alembic chain, seeded the admin, and brought all 3 containers Up; `/openapi.json`→`EndpointDLP`, UI→200, admin login returned a JWT.

## C.2 Open VM→dev-box connectivity (NAT + firewall)

With VMware **NAT**, the VM reaches the dev box at the host's **VMnet8** adapter IP. Find it on the dev box:

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object InterfaceAlias -like 'VMware*VMnet8*' | Select IPAddress
# author's dev box: 192.168.6.1
```

Docker publishes port 8000 on all interfaces; allow it through the dev-box firewall (elevated):

```powershell
New-NetFirewallRule -DisplayName 'DLP Server 8000' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Any
```

From the **VM**, confirm reachability:

```powershell
Test-NetConnection 192.168.6.1 -Port 8000   # expect TcpTestSucceeded : True
```

The admin can drive the UI from the **dev box's own browser** (`http://localhost/`), so a port-80 rule is only needed to open the UI *from the VM*. Undo later with `Remove-NetFirewallRule -DisplayName 'DLP Server 8000'`.

> ✅ **MANUALLY TESTED** (2026-06-26) — the `New-NetFirewallRule` rule was added on the dev box and the live VM→`192.168.6.1:8000` round-trip (`Test-NetConnection`) succeeded.

## C.3 Create the agent in the UI (admin enrollment), copy its UUID

1. In the UI: **Agents → create**. Enter a hostname; set status **`inactive`** (so you can watch it flip to ACTIVE on the first heartbeat). Save.
2. Copy the agent's **UUID** shown under the hostname.
3. *(Optional)* **Policies → create** a policy and assign it to this agent (or its group) so you can see it delivered to the VM's `policies.yaml`.

> ✅ **MANUALLY TESTED** (2026-06-26) — logging in and creating the agent (then copying its UUID) was verified end-to-end. **Step 3 (the optional policy create + assign) was NOT exercised** — server-pushed policies are delivered but do not enforce on the current agent, so it is deferred to Phase 1 (`agent-server-integration-plan.md`).

## C.4 Point the agent at the server (VM), restart

Edit the **installed** config **`C:\Program Files\DLP\config.yaml`** → `server:` block:

```yaml
server:
  url: "http://192.168.6.1:8000"   # BARE origin; the agent appends /api/v1 itself
  agent_id: "<UUID copied in C.3>"
  heartbeat_interval: 30
  log_sync_interval: 300
  enabled: true                    # flips standalone → cloud mode
```

The `server:` block is **restart-only** (it is not hot-reloadable — see the Appendix). Apply it:

```powershell
Restart-Service DLPAgent
```

**Order matters:** complete C.1–C.2 (server up, firewall open, probe green) **before** the restart. If the server is unreachable when the agent starts and an `agent_id` is set, the agent keeps the configured id and retries on the heartbeat loop (it will go ACTIVE once the server is reachable).

## C.5 Verify the connection

**On the VM:**

```powershell
Get-Content C:\ProgramData\DLP\logs\dlp-agent.log -Tail 60   # expect "Cloud bridge: started (agent_id=…)" + recurring heartbeats
```

**On the dev box (UI):**
- **Agents** page: the row shows **ACTIVE** with `last_seen` advancing every ~30 s.
- **Agent Logs** page: log tails pushed from the VM appear.
- If you assigned a policy in C.3: the VM's `C:\Program Files\DLP\analyzer\policies.yaml` gains the auto-generated header + the delivered policy after a heartbeat.

> ✅ **MANUALLY TESTED** (2026-06-26) — on the VM the agent connected after the `config.yaml` `server:` edit + `Restart-Service DLPAgent`: `dlp-agent.log` showed the cloud-bridge start + recurring heartbeats, and the console **Agents** page showed the row **ACTIVE** with `last_seen` advancing and pushed log tails on **Agent Logs**. The last bullet (an assigned policy appearing in the VM's `policies.yaml`) was **NOT** exercised — deferred to Phase 1 with the rest of policy delivery/enforcement.

## C.6 Teardown / back to standalone

- **Disconnect an agent:** set `server.enabled: false` in `C:\Program Files\DLP\config.yaml` + `Restart-Service DLPAgent` (pure standalone again).
- **Stop the server (clean):** from `src\management_console`, `docker compose down -v` (removes containers + network **and** the DB volume — next `up` starts from a fresh, re-seeded DB). Drop the `-v` only if you want to keep the DB between runs. Optional: `docker image rm management_console-backend management_console-frontend`.
- **Firewall:** `Remove-NetFirewallRule -DisplayName 'DLP Server 8000'`.

---

## Appendix — file & config quick reference

| What | Source (repo, gets installed) | Installed (dev box **or** VM) | Automated tests |
|---|---|---|---|
| Config | `<RepoRoot>\config.yaml` | `C:\Program Files\DLP\config.yaml` | generated `tmp\harness\<uuid>\config.yaml` |
| Policies | `<RepoRoot>\analyzer\policies.yaml` | `C:\Program Files\DLP\analyzer\policies.yaml` | `scripts\harness\fixture_policies\*.yaml` |
| Logs | n/a (not run from source) | `C:\ProgramData\DLP\logs\` | `tmp\harness\<uuid>\DLP\logs\` |
| Apply policy/config edits | rebuild/reinstall to apply | save (auto) or `dlp-ctl reload` | n/a |

`config.yaml` sections: `data_pipe`/`ctl_pipe`/`admin_pipe` (named pipes, **restart-only**), `pools` (worker counts, restart-only), `limits.max_file_bytes` (browser upload cap), `analyzer.max_extracted_chars` (also governs clipboard text), `analyzer.supported_extensions` (file types the analyzer scans; others → `unsupported_format` + `failure_mode`), `supervisor` (restart policy, restart-only), `service` (`drain_timeout_seconds`, `analysis_timeout_seconds`), `paths` (artifact locations — **Debug** by default, restart-only), `proxy` (restart-only), `policies_file` (restart-only), `install` (`install_root`, `service_start_type: auto`, …), and per-component sections, each read only by its own client:
- `clipboard` — `pipe_timeout_ms`, `failure_mode`. (There is **no** copied-text byte cap: the full clipboard text is sent and `analyzer.max_extracted_chars` decides whether it is scanned.)
- `browser` — `pipe_timeout_ms`, `failure_mode` (default `fail_closed`). The upload filters (`extensions`/`mime_types`/`domain_blocklist`/`upload_url_keywords`), `min_upload_size_bytes`, and `temp_dir` are **hardcoded** in `interceptors/browser/config.py` (rarely changed; not admin-exposed — `temp_dir` resolves to the system `%TEMP%`).
- `peripheral_storage` split into `controller:` (the DLL-injector/`NtCreateFile` hook — `failure_mode`, `target_processes`, `shared_memory_name`, `payload_dll_path`, `in_user_session`) and `transfer_agent:` (`connect_timeout_ms`, `analysis_timeout_ms`, `failure_mode`).

`failure_mode` (`fail_closed`→BLOCK default | `fail_open`→ALLOW) is unified across channels for every analysis/pipe failure.

**Hot-reloadable** (apply on `dlp-ctl reload` or `config.yaml` save, **no restart**): every per-component client field above, plus the orchestrator's own `failure_mode`, `limits.max_file_bytes`, `analyzer.max_extracted_chars`, `analyzer.supported_extensions`, `service.analysis_timeout_seconds`, and `service.drain_timeout_seconds`. (Before this change the orchestrator froze its own settings at startup and only the client sections + App Control reloaded — that gap is fixed; see `orchestrator/config.py::apply_hot_reload`.) **Restart-only:** the pipe names, `pools`/`pipe_listeners`, `paths.*`, `proxy.*`, `policies_file`, `supervisor.*`, `install.*`, and `server.*` (the Management Console connection — see §C; `agent_id` changing mid-flight would break heartbeat identity, so it is read once at startup) (the agent logs a "requires restart" warning if a pipe name is changed). `peripheral_storage.controller.shared_memory_name` is likewise rejected at runtime by the Controller.

**Policy rules live separately in `analyzer/policies.yaml`** (policy ≠ config). Each policy may set a `user_message:` — the end-user block reason (in **English**) shown on the browser popup, the clipboard replacement text, and the Transfer Agent Note (the policy `id` is never shown). It is hot-reloadable via `dlp-ctl reload`. Failure-mode blocks (timeout/oversize/text_cap/unsupported/…) instead show a per-category English message defined in `orchestrator/messages.py`. **All end-user notifications across every channel are in English.**
