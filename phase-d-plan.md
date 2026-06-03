# Phase D — Installer / Uninstaller

> **Cross-reference key:**
> - **IT-D1 … IT-D8** are the implementation tasks (ordered so each commit point compiles + runs).
> - **Q1 … Q4** are user-confirmed Phase D decisions (build source, ShellExt scope, service install, Python runtime).
> - **R1 … R7** are tracked risks.
> - **install_root** = `%ProgramFiles%\DLP\` (the directory `python -m orchestrator --install` populates). **state_dir** = `%ProgramFiles%\..\..\ProgramData\DLP\state\` (machine-writable; holds uninstall manifest + CA thumbprint + proxy backup). **CLSID** = `{B3A1C2D4-E5F6-7890-ABCD-EF1234567890}` (the ShellExt COM class, defined in `interceptors\peripheral_storage\ShellExtension\DlpContextMenu.h:9-13`). **DACL** = discretionary access control list on the named pipe (set up in Phase C fix #1). **CA** = the mitmproxy-generated certificate authority that signs intercepted HTTPS.

## Context

End of Phase C: `python -m orchestrator --foreground` is a complete one-command DLP endpoint for dev. But it still runs from the source tree, depends on a hand-built `.venv`, relies on `interceptors\peripheral_storage\verify-install.ps1` to register the ShellExtension COM, and has no Windows service installed. None of that is deployable.

Phase D closes the gap **for installable-machine mode** (LocalSystem service-mode is still Phase E):

1. `python -m orchestrator --install` (elevated) stands up a complete endpoint under `%ProgramFiles%\DLP\` from pre-built artifacts. After it returns, `sc query DLPAgent` works, the "Transfer to USB (DLP Protected)" context menu is visible to all logged-on users, the mitmproxy CA is in the LocalMachine Root store, and the invoking user's HKCU has its proxy keys redirected to `127.0.0.1:8080` (with the original values backed up under `state_dir`).
2. `python -m orchestrator --uninstall` reverses everything **idempotently** — each undo step succeeds whether its target is present or already gone, so a partial install can always be cleaned up.
3. The legacy `interceptors\peripheral_storage\verify-install.ps1` becomes a deprecated tombstone: prints a banner pointing at `--install` and exits unless an opt-in env var is set.
4. The Windows service is registered but its `SvcDoRun` body is a placeholder (logs + blocks on stop event). The real service body lands in Phase E once cross-session injection is resolved — see Q3.

**Out of scope for Phase D** (so it isn't relitigated during implementation):
- Real service-mode behavior + `CreateProcessAsUser` + WTS session change handling — all Phase E.
- Firefox CA trust — Firefox uses its own NSS-based per-profile trust store and is **explicitly out of project scope**. Chrome and Edge consume the Windows LocalMachine Root store via the enterprise-managed-roots mechanism, so the Phase D `certutil -addstore Root` step covers both without per-profile work.
- ARM64 Windows. The native binaries are x64-only (Payload.vcxproj + DlpShellExt.vcxproj target x64); the installer aborts on non-AMD64 with a clear message — see R4.

## Locked decisions (this session)

| # | Decision | Rationale |
|---|----------|-----------|
| Q1 | **Expect pre-built artifacts; fail loudly with absolute paths if any are missing.** Installer reads paths from `config.yaml`'s `paths:` section, resolves them against the dev tree root, asserts `Path(...).is_file()` per artifact, aborts at the first miss. A separate developer-side script (sketch in IT-D8) builds them; Phase D core does not invoke `dotnet publish` / `msbuild`. | Keeps "install" a packaging operation. Build-vs-install separation matches Visual Studio 2026 developer workflow. |
| Q2 | **HKLM machine-wide ShellExtension registration.** Installer runs elevated and writes `HKLM\Software\Classes\CLSID\{B3A1C2D4-...}`, `HKLM\Software\Classes\*\shellex\ContextMenuHandlers\DLPTransfer`, `HKLM\Software\Classes\Directory\shellex\ContextMenuHandlers\DLPTransfer`, plus `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Shell Extensions\Approved\{...}` and `HKLM\SOFTWARE\DLPAgent\TransferAgentPath`. | Matches the Phase E LocalSystem-service install model: one registration covers every user-session `explorer.exe`. The existing `DlpContextMenu.cpp:255-258` already tries HKLM first, so the C++ source needs no change. |
| Q3 | **Register the `DLPAgent` Windows service in Phase D, but its `SvcDoRun` is a placeholder.** It calls `configure_logging`, writes a CRITICAL-level event-log line "Phase D placeholder; run `--foreground` for actual operation", and blocks on `WaitForSingleObject(hWaitStop, INFINITE)`. Phase E replaces the body. | Satisfies integration-plan2.md's Phase D done-when (`sc query DLPAgent` works after install + cleans on uninstall) without pre-committing to the still-open Phase E question of whether Controller can inject from LocalSystem. |
| Q4 | **Bundle a pre-prepared Python 3.13 embeddable distribution** at `<install_root>\python\`. The dev tree gains `python-embed\` (gitignored) populated by a one-time `scripts\prepare-python-embed.ps1`; the installer just `shutil.copytree`s it. | Self-contained deployment with no system-Python dependency. The embeddable's quirks (no pip by default, no `site-packages` discovery, `pywintypes313.dll` placement) are owned by the prep script, not the installer — see R1 and IT-D8. |

## Critical files

**Edits** (line refs are post-Phase-C):
- `orchestrator\__main__.py:38-42` — replace the three "Not implemented yet." prints in the `--install` / `--uninstall` / `--service` argparse branch with dispatches to `installer.run_install`, `installer.run_uninstall`, `service.run_as_service`. Add the imports near the existing Phase C `from orchestrator.supervisor import ...` line (~line 20).
- `orchestrator\config.py:10-34` — append three new `OrchestratorConfig` dataclass fields **after** the existing `policies_file: str` (preserves the existing positional-init order used everywhere): `transfer_agent_exe: str`, `shell_extension_dll: str`, `payload_dll: str`. At `:49-73` (the `load_config()` body) read those three keys from the `paths:` dict with the defaults shown in §3 below. The install-only settings (service name, state_dir, manifest filename) are read **directly from `OrchestratorConfig.raw['install']`** inside `installer.py` — no dataclass field, since `--foreground` doesn't need them.
- `config.yaml:24` — drop the "Phase D will add transfer_agent_exe / shell_extension_dll." comment; add the three new `paths:` keys and the new `install:` top-level section (full schema in §3 below).
- `interceptors\peripheral_storage\verify-install.ps1` — replace the entire body with a 5-line tombstone banner that exits 1 unless `$env:DLP_ALLOW_LEGACY_INSTALL` is set. Reason for keeping rather than deleting: emergency rollback path if a Phase D regression breaks installs; the file is ~95 lines so the cost of carrying it is trivial.

**New** (implementation session writes these):
- `orchestrator\installer.py` — the transactional install/uninstall driver, ~400 lines. Full API in §4.
- `orchestrator\service.py` — pywin32 ServiceFramework subclass with placeholder `SvcDoRun`, ~70 lines. Full code sketch in §5.
- `scripts\prepare-python-embed.ps1` — developer prep script, sketch in §7.
- `scripts\prepare-install-payload.ps1` — developer prep that builds C# + C++ and stages a `dist\DLP\` mirror of the install tree, sketch in §7.

**Reused (no source changes — important for context):**
- `interceptors\peripheral_storage\ShellExtension\DlpContextMenu.cpp:255-258` — already tries `HKEY_LOCAL_MACHINE` first when resolving `TransferAgentPath`. **No C++ changes needed** for Q2; the installer writing the HKLM key is enough.
- `interceptors\peripheral_storage\ShellExtension\dllmain.cpp:71-100` — the DLL's own `DllRegisterServer` / `DllUnregisterServer` exports are **not invoked** by `--install`. Reason: doing the keys from Python gives us deterministic key strings to record in the uninstall manifest, and lets us write the `Approved` key reliably (the C++ version silently swallows failures when not elevated). We never call `regsvr32.exe DlpShellExt.dll`.
- `interceptors\peripheral_storage\Controller\Controller.csproj:31-40` — already copies `Payload.dll` next to `UsbDlpController.exe` at build time. Means the dev-tree resolved path for `payload_dll` is `interceptors\peripheral_storage\Controller\bin\Debug\net10.0-windows\win-x64\Payload.dll`; the installer just copies the entire Controller publish dir.

## Implementation tasks

Ordered so every commit point compiles + the existing `--foreground` flow keeps working unchanged.

### IT-D1. Schema additions: `config.yaml` + `OrchestratorConfig`

**Goal:** new paths and install settings are readable; nothing yet wires them.

Add to `config.yaml` (under `paths:`, between `controller_exe` and `log_dir`):

```yaml
paths:
  # existing (unchanged)
  mitmdump_exe: ""
  addon_script: "interceptors/browser/addon.py"
  clipboard_exe: "src/ClipboardInterceptor/bin/Debug/net10.0-windows/ClipboardInterceptor.exe"
  controller_exe: "interceptors/peripheral_storage/Controller/bin/Debug/net10.0-windows/win-x64/UsbDlpController.exe"
  log_dir: ""

  # Phase D additions — used by --install to source artifacts; used by --foreground/Supervisor
  # only for transfer_agent_exe (TransferAgent is spawned by ShellExtension, not Supervisor, but
  # the path lives here for single-source-of-truth + future Phase E session bridging).
  transfer_agent_exe: "interceptors/peripheral_storage/TransferAgent/bin/Debug/net10.0-windows/win-x64/DlpTransferAgent.exe"
  shell_extension_dll: "interceptors/peripheral_storage/out/ShellExtension/Debug/DlpShellExt.dll"
  payload_dll: "interceptors/peripheral_storage/Payload/x64/Debug/Payload.dll"

# new top-level section — read only by installer.py via OrchestratorConfig.raw['install']
install:
  install_root: ""                              # empty → %ProgramFiles%\DLP
  state_dir: ""                                 # empty → %PROGRAMDATA%\DLP\state
  mitmproxy_confdir: ""                         # empty → %PROGRAMDATA%\DLP\mitmproxy
  service_name: "DLPAgent"
  service_display_name: "DLP Endpoint Agent"
  service_description: "Endpoint DLP orchestrator (Phase D placeholder; full session-aware behavior arrives in Phase E)."
  ca_thumbprint_file: "installed_ca.txt"        # relative to state_dir
  proxy_backup_file: "proxy_backup.json"        # relative to state_dir
  install_manifest_file: "install_manifest.json" # relative to state_dir
```

`OrchestratorConfig` gains exactly **three new fields** (`transfer_agent_exe`, `shell_extension_dll`, `payload_dll`) so `--foreground` Supervisor + Phase E can resolve them via the existing path-resolution helpers. The install-only settings are read directly from `cfg.raw['install']` inside `installer.py` — keeps the dataclass minimal.

**Why install settings stay out of the dataclass:** they have no consumer in the `--foreground` hot path. Putting them in `OrchestratorConfig` would bloat every test fixture (`scripts\harness\conftest.py:_minimal_config`) for zero benefit. The `cfg.raw` access pattern matches how `ctl_server.py` already projects per-component sections.

**Validates:** `python -c "from orchestrator.config import load_config; c=load_config(); print(c.transfer_agent_exe, c.shell_extension_dll, c.payload_dll, c.raw['install']['service_name'])"` prints the four values.

### IT-D2. `orchestrator\installer.py` — transactional driver

**Goal:** the install/uninstall engine exists; not wired to `__main__.py` yet.

API:

```python
# orchestrator/installer.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Any

@dataclass
class InstallContext:
    config_path: Path                    # source-tree config.yaml
    dev_root: Path                       # source-tree root (Path(config_path).parent)
    install_root: Path                   # %ProgramFiles%\DLP
    state_dir: Path                      # %PROGRAMDATA%\DLP\state
    log_dir: Path                        # %PROGRAMDATA%\DLP\logs
    mitm_confdir: Path                   # %PROGRAMDATA%\DLP\mitmproxy
    service_name: str
    service_display: str
    service_desc: str
    artifacts: dict[str, Path]           # resolved source-tree paths, validated to exist
    manifest: list[dict] = field(default_factory=list)   # appended after each successful step

@dataclass
class Step:
    id: str
    do:   Callable[[InstallContext], dict[str, Any] | None]   # returns undo payload (may be None)
    undo: Callable[[InstallContext, dict[str, Any] | None], None]   # idempotent

# Public entry points — wired into __main__.py in IT-D5
def run_install(config_path: Path | None) -> int: ...
def run_uninstall(config_path: Path | None) -> int: ...

# Internals (each step is one (do, undo) pair)
def _step_require_admin()      -> Step: ...
def _step_check_arch()         -> Step: ...
def _step_verify_artifacts()   -> Step: ...
def _step_make_dirs()          -> Step: ...
def _step_copy_payload()       -> Step: ...
def _step_bootstrap_ca()       -> Step: ...
def _step_install_root_ca()    -> Step: ...
def _step_backup_proxy()       -> Step: ...
def _step_set_proxy()          -> Step: ...
def _step_register_shellext()  -> Step: ...
def _step_notify_shell()       -> Step: ...
def _step_install_service()    -> Step: ...
```

**Driver behavior:**

`run_install` flow:
1. Load `config.yaml` (uses the existing `load_config`).
2. Build `InstallContext` from the loaded config + `cfg.raw['install']` overrides.
3. Iterate the step list. For each step: call `do(ctx)`, append `{"id": step.id, "undo_payload": result}` to `ctx.manifest`, write `ctx.manifest` to `state_dir\install_manifest.json` (per-step persistence → a crash between steps still has data for `--uninstall`).
4. **On any exception in a `do`:** log full traceback, then iterate `ctx.manifest` in reverse calling each step's `undo` (catch + log per-undo exceptions but don't re-raise; rollback is best-effort). Return exit code 1.
5. On full success, log `"Install complete. Run `sc start DLPAgent` to start the service (placeholder body; expect immediate idle)."` Return 0.

`run_uninstall` flow:
1. Load `state_dir\install_manifest.json`. If missing: synthesize a manifest from the source-tree config defaults (every step's `undo` is idempotent; missing payload just means "use defaults"). Log INFO if synthesized.
2. Iterate manifest **in reverse** calling each step's `undo`. Each `undo` wraps its body in `try/except (FileNotFoundError, OSError) as e:` and logs at INFO with the step id. Continue past failures.
3. After all undos: delete `install_manifest.json` itself. Return 0.

**Manifest schema** (one entry per completed step):
```json
{"id": "register_shellext", "undo_payload": {"keys": ["HKLM\\Software\\Classes\\CLSID\\{B3A1C2D4-...}", ...]}}
```

**Validates:** `python -c "from orchestrator.installer import run_install, run_uninstall; print('ok')"` imports cleanly.

### IT-D3. Concrete install steps (do / undo)

Ordered to match the manifest's forward order. Each step lives in `installer.py` as a function returning a `Step`.

| # | step.id | `do` action | failure mode | `undo` action | undo "already absent" handler |
|---|---------|-------------|--------------|----------------|--------------------------------|
| 1 | `require_admin` | `if not ctypes.windll.shell32.IsUserAnAdmin(): raise SystemExit(2, "Re-run from an elevated prompt.")` | `SystemExit(2)` | no-op | n/a |
| 2 | `check_arch` | `if platform.machine() != "AMD64": raise SystemExit(3, "x64-only; ARM64 build pipeline TBD.")` | `SystemExit(3)` | no-op | n/a |
| 3 | `verify_artifacts` | Resolve each `paths.*` key against `dev_root`. For each: assert `.is_file()`. Also verify `dev_root\python-embed\python.exe` exists. | `FileNotFoundError(abs_path)` | no-op | n/a |
| 4 | `make_dirs` | `os.makedirs(install_root, ...)`, `bin\Controller`, `bin\TransferAgent`, `bin\Clipboard`, `bin\ShellExt`, `python\`, `state_dir`, `log_dir`, `mitm_confdir`. Record `created=[paths that we freshly made]`. | `OSError` | For each `created` in reverse: `os.rmdir` if empty; if not empty, log WARN and skip. | `FileNotFoundError` ignored. |
| 5 | `copy_payload` | `shutil.copytree`: <br>• dev_root/`orchestrator` → install_root/`orchestrator` <br>• `analyzer` → `analyzer` <br>• `interceptors/browser` → `interceptors/browser` <br>• `python-embed` → `python/` <br>Then **per-component** copy of each .NET publish dir contents into `bin/<Component>/`: full directory contents of each publish folder (so .deps.json + .runtimeconfig.json + native DLLs all land alongside the .exe). `Payload.dll` ships inside the Controller publish dir already (the .csproj's `CopyPayloadDll` target). `DlpShellExt.dll` is a single-file copy into `bin/ShellExt/`. Finally: copy `config.yaml` → install_root\config.yaml **with paths.* rewritten to install-mode strings** (see §3 schema rewrite). | `shutil.Error` | `shutil.rmtree(install_root, ignore_errors=True)`. | `FileNotFoundError` → skip. |
| 6 | `bootstrap_ca` | `subprocess.run([install_root/python/python.exe, "-m", "mitmproxy.tools.dump", "--no-server", "--set", f"confdir={mitm_confdir}"], timeout=10)` — `--no-server` is the documented mitmproxy global option to skip binding the proxy port; mitmproxy still generates `mitmproxy-ca.pem` + the derived `mitmproxy-ca-cert.cer` in the confdir during startup (verified via [mitmproxy certs docs](https://docs.mitmproxy.org/stable/concepts/certificates/)). Poll `mitm_confdir/mitmproxy-ca-cert.cer` every 200 ms; once it exists, kill the child. If the file doesn't appear within 10 s, abort with the captured stderr. | `subprocess.TimeoutExpired` w/o cert produced | no-op (the dir + cert files are cleaned by `make_dirs`'s undo + a `_step_cleanup_mitm_confdir` if we add one — see notes below). | n/a |
| 7 | `install_root_ca` | Compute the SHA1 fingerprint of `mitmproxy-ca-cert.cer` using stdlib `hashlib` + DER parsing (avoids the `cryptography` package's extra dependency surface — mitmproxy ships it but installer code should not assume it). Save the hex thumbprint to `state_dir/installed_ca.txt`. Then `subprocess.run(["certutil", "-addstore", "-f", "Root", str(cer_path)], check=True)` — verified syntax against [Microsoft Learn: certutil](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/certutil); `-addstore -f Root` adds to the **LocalMachine** Root store under admin context. | non-zero certutil exit | Read thumbprint from `installed_ca.txt`; run `certutil -delstore Root <thumbprint>`. Treat exit `0x80092004` (`CRYPT_E_NOT_FOUND`) as success. | If `installed_ca.txt` missing → log WARN with explicit cleanup hint pointing at `certmgr.msc → Trusted Root → mitmproxy`. |
| 8 | `backup_proxy` | Read `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings` values `ProxyEnable` (DWORD), `ProxyServer` (string), `ProxyOverride` (string) for the **installing user**. Save JSON `{"ProxyEnable": <int or null>, "ProxyServer": <str or null>, "ProxyOverride": <str or null>}` to `state_dir/proxy_backup.json`. Missing values → `null` (Internet Settings often omits values entirely for never-configured fields). | `OSError` writing the backup file | no-op (the proxy restore lives in step 9's undo) | n/a |
| 9 | `set_proxy` | `winreg.SetValueEx` on the three values: `ProxyEnable=1`, `ProxyServer="127.0.0.1:<proxy_listen_port>"` (from config; default 8080), `ProxyOverride="<config.proxy_bypass>"`. **Phase 3 scope:** installing user's HKCU only. Phase E extends to other sessions via WTS enumeration. | `OSError` | Restore from `proxy_backup.json`: set each value back to its backed-up value, or `winreg.DeleteValue` if the backup said null. Then `subprocess.run(["netsh","winhttp","reset","proxy"], check=False)` so any WinHTTP-using service (Windows Update, etc.) picks up the change. | Backup file missing → delete the three values we set; treat each `FileNotFoundError` as success. |
| 10 | `register_shellext` | Write (all under HKLM\Software\Classes — explicit not via HKCR): <br>• `CLSID\{B3A1C2D4-...}\@="DLP File Transfer"` <br>• `CLSID\{B3A1C2D4-...}\InProcServer32\@=<install_root>\bin\ShellExt\DlpShellExt.dll` <br>• `CLSID\{B3A1C2D4-...}\InProcServer32` value `ThreadingModel="Apartment"` <br>• `*\shellex\ContextMenuHandlers\DLPTransfer\@="{B3A1C2D4-...}"` <br>• `Directory\shellex\ContextMenuHandlers\DLPTransfer\@="{B3A1C2D4-...}"` <br>Plus under HKLM root: <br>• `SOFTWARE\Microsoft\Windows\CurrentVersion\Shell Extensions\Approved` value `{B3A1C2D4-...}="DLP File Transfer"` (the Approved key is what Explorer consults under SAFER policies). <br>• `SOFTWARE\DLPAgent` value `TransferAgentPath=<install_root>\bin\TransferAgent\DlpTransferAgent.exe` (matches `DlpContextMenu.cpp:11,254-258`). <br>Record every (hive, key, value) we wrote in the undo payload. | `PermissionError` if not admin (step 1 prevents) | For each recorded key in reverse: `winreg.DeleteKey` (or `DeleteValue` for the two single-value writes). Catch `FileNotFoundError` and continue. | `FileNotFoundError` → log INFO + skip. |
| 11 | `notify_shell` | `ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED=0x08000000, SHCNF_IDLIST=0, None, None)`. Forces Explorer to re-enumerate context-menu handlers without restart. | n/a | Same call on uninstall. | n/a |
| 12 | `install_service` | `subprocess.run(["sc.exe", "create", service_name, f"binPath= \"{install_root}\\python\\python.exe\" -m orchestrator --service --config \"{install_root}\\config.yaml\"", "start=", "demand", "DisplayName=", service_display], check=True)`. Then `sc.exe description {service_name} "{service_desc}"`. Avoid `win32serviceutil.HandleCommandLine` for the install action — it wants to register `PythonService.exe` as the host, which the embeddable distribution doesn't ship. Bare `sc create` with the explicit python.exe is cleaner and survives Phase E moving the body around. (Reference: [pywin32 win32serviceutil.py](https://github.com/mhammond/pywin32/blob/main/win32/Lib/win32serviceutil.py) confirms `HandleCommandLine` is the right entry for the in-service dispatch — used in `service.py` per §5 — but not for `--install`-side registration when using a custom python host.) | non-zero `sc.exe` exit; exit 1073 (`ERROR_SERVICE_EXISTS`) → run `sc.exe config` instead and treat as success | `sc.exe stop {service_name}` (best effort), then `sc.exe delete {service_name}`. | Exit 1060 (`ERROR_SERVICE_DOES_NOT_EXIST`) → success. Exit 1062 (`ERROR_SERVICE_NOT_ACTIVE`) for the stop → success. |

**Quoting note for step 12:** `sc create` is famously picky about argument formatting — the space after `binPath=` is **required** by sc.exe parsing, and nested quotes inside `binPath=` must be doubled-quoted. The Python invocation passes the binPath as one argv element (`subprocess.run` argv list, not shell), so Windows's CreateProcess receives it correctly. Verified syntactically against [Microsoft Learn: sc create](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/sc-create).

**Config rewrite during step 5** (cause→effect): the installed `config.yaml`'s `paths:` strings change from dev-relative (`"interceptors/peripheral_storage/Controller/bin/Debug/.../UsbDlpController.exe"`) to install-relative (`"bin/Controller/UsbDlpController.exe"`). Same shape, just shorter. The other sections (`pools`, `limits`, `proxy`, `clipboard`, `browser`, `peripheral_storage`) are copied byte-for-byte. The installed `paths.mitmdump_exe` becomes `"python/Scripts/mitmdump.exe"` (mitmproxy's pip entry point inside the embeddable). The installed `paths.log_dir` stays `""` so `logging_setup.py` falls back to `%PROGRAMDATA%\DLP\logs`.

### IT-D4. `orchestrator\service.py` placeholder

**Goal:** SCM can start + stop the service; nothing happens inside.

```python
# orchestrator/service.py
"""Phase D placeholder Windows service.

Registers as DLPAgent; SvcDoRun logs and blocks on hWaitStop. Phase E will
fold in the real foreground loop (Supervisor + pipes + session bridge).
"""
from __future__ import annotations
import logging
import socket
import sys

import servicemanager        # pywin32
import win32event
import win32service
import win32serviceutil

from orchestrator.logging_setup import configure_logging


class DLPAgentService(win32serviceutil.ServiceFramework):
    _svc_name_         = "DLPAgent"
    _svc_display_name_ = "DLP Endpoint Agent"
    _svc_description_  = ("Endpoint DLP orchestrator (Phase D placeholder; "
                          "the service is registered but does no DLP work yet. "
                          "Run `python -m orchestrator --foreground` until Phase E lands.")

    def __init__(self, args):
        super().__init__(args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        socket.setdefaulttimeout(60)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        configure_logging(foreground=False)
        log = logging.getLogger("orchestrator.service")
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_WARNING_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_,
             " (PLACEHOLDER: run `python -m orchestrator --foreground` for actual DLP)"),
        )
        log.warning("DLPAgent placeholder started. Phase E will replace this body.")
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
        log.info("DLPAgent placeholder stopped.")


def run_as_service() -> None:
    """Entry point from `python -m orchestrator --service`.

    When SCM launches us, argparse has consumed `--service` and sys.argv[1:] is empty.
    We hand off to pywin32's PrepareToHostSingle + StartServiceCtrlDispatcher — the
    canonical SCM-side dispatch (verified against
    https://github.com/mhammond/pywin32/blob/main/win32/Lib/win32serviceutil.py).
    """
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(DLPAgentService)
    servicemanager.StartServiceCtrlDispatcher()


if __name__ == "__main__":
    # Direct invocation (debugging only): standard HandleCommandLine UX.
    win32serviceutil.HandleCommandLine(DLPAgentService)
```

**Why the in-process `PrepareToHostSingle` path** (cause→effect): SCM invokes our `binPath` which is `python.exe -m orchestrator --service`. Argparse in `__main__.py` strips `--service`. By the time `run_as_service` is called, `sys.argv` is `[<...>\python.exe]`-shaped. `PrepareToHostSingle` is the documented pywin32 path for hosting a single service in the current Python interpreter without `PythonService.exe`. The `__main__` block of `service.py` is only reached by direct invocation (e.g., for in-place debugging via `python -m orchestrator.service install`).

### IT-D5. Wire `__main__.py` dispatch

**Goal:** `python -m orchestrator --install` runs `installer.run_install`; `--uninstall` runs `installer.run_uninstall`; `--service` runs `service.run_as_service`.

Edits to `orchestrator\__main__.py`:

- Add imports near the existing Phase C imports (after `from orchestrator.supervisor import ...`):
  ```python
  from orchestrator.installer import run_install, run_uninstall
  from orchestrator.service import run_as_service
  ```
- Replace lines 38-42 (the current "Not implemented yet." print + return for each of `--install`/`--uninstall`/`--service`):
  ```python
  if args.install:
      sys.exit(run_install(args.config))
  if args.uninstall:
      sys.exit(run_uninstall(args.config))
  if args.service:
      run_as_service()                  # SCM dispatch; only returns when service stops
      sys.exit(0)
  ```

**Validates:** `python -m orchestrator --install --help` does NOT print "Not implemented yet." anymore; running it elevated triggers the install steps; running it non-elevated exits 2 with the elevation message.

### IT-D6. Deprecate `verify-install.ps1`

**Goal:** legacy script can no longer accidentally run; clear redirect to `--install`.

Replace the entire body of `interceptors\peripheral_storage\verify-install.ps1` with:

```powershell
Write-Host ""
Write-Host "*** verify-install.ps1 is DEPRECATED. ***" -ForegroundColor Yellow
Write-Host "Use:  python -m orchestrator --install"
Write-Host ""
Write-Host "If you really need the old per-user script for emergency rollback,"
Write-Host "set DLP_ALLOW_LEGACY_INSTALL=1 and re-run. The legacy code is kept at"
Write-Host "git history commit <last-pre-phase-D-sha> (run: git log -- $PSCommandPath)."
Write-Host ""
if (-not $env:DLP_ALLOW_LEGACY_INSTALL) { exit 1 }
Write-Host "DLP_ALLOW_LEGACY_INSTALL set — refusing anyway. Recover legacy script from git history."
exit 1
```

Reason: deletion is more destructive than a tombstone; the file is small; emergency rollback via git history works. (User feedback memory: prefer single bundled changes; this is one logical commit point.)

### IT-D7. Verification harness

**Goal:** one cheap automated regression so a future refactor of `installer.py` cannot silently break uninstall idempotency.

Add `scripts\harness\test_installer.py` exercising the **stepwise rollback** path with a no-op step list (no real registry, no real cert, no real service — just `tempfile.TemporaryDirectory` as the install root + a synthetic step that succeeds, fails, or no-ops). Verifies:
- Forward run with all steps succeeding → manifest written + every artifact present.
- Forward run with step N raising → undo runs for steps N-1 .. 0 in reverse + return code 1.
- `run_uninstall` with no manifest → synthesises defaults + completes with return 0.
- `run_uninstall` then `run_uninstall` again → second pass is no-op (every undo handler catches missing-target errors).

**No real Windows-elevation, registry, or service tests** — those are manual smoke (§9). The harness test only verifies the *driver*, not the per-step Win32 plumbing. This matches Phase C's IT-C5 philosophy (one cheap lifecycle test; manual smoke for the load-bearing assertions).

### IT-D8. Developer-side prep scripts (sketches)

Both are **out of the Phase D core delivery** but are mentioned here because the installer cannot run without their output. Implementation pulls them in as low-effort placeholders that can be filled in over time.

**`scripts\prepare-python-embed.ps1`** — produces `<dev_root>\python-embed\` once per dev machine:
1. `Invoke-WebRequest https://www.python.org/ftp/python/3.13.x/python-3.13.x-embed-amd64.zip` (pin the exact 3.13.x patch version the team standardizes on; the variable lives at the top of the script).
2. `Expand-Archive` to `<dev_root>\python-embed\`.
3. Patch `<dev_root>\python-embed\python313._pth` — uncomment the `import site` line and append `Lib\site-packages`. Confirmed pattern from [Python docs / embedded distribution](https://docs.python.org/3/using/windows.html#the-embeddable-package).
4. Download `get-pip.py` and run `python-embed\python.exe get-pip.py`.
5. `python-embed\python.exe -m pip install -r ..\requirements.txt`.
6. Copy `Lib\site-packages\pywin32_system32\pywintypes313.dll` and `pythoncom313.dll` to `python-embed\` (or write a `sitecustomize.py` that calls `os.add_dll_directory` against `pywin32_system32`). **R1** captures the trade-off.

**`scripts\prepare-install-payload.ps1`** — runs the existing Debug builds, then mirrors a `dist\DLP\` tree matching the install layout (so `python -m orchestrator --install --config dist\DLP\config.yaml` can be tested end-to-end without admin-installing every dev iteration):
```powershell
# from Developer PowerShell at repo root
dotnet build src\ClipboardInterceptor\ClipboardInterceptor.csproj          # net10.0-windows
dotnet build interceptors\peripheral_storage\Controller\Controller.csproj  # net10.0-windows/win-x64; auto-copies Payload.dll
dotnet build interceptors\peripheral_storage\TransferAgent\DlpTransferAgent.csproj
& "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe" `
    interceptors\peripheral_storage\Payload\Payload.vcxproj /p:Configuration=Debug /p:Platform=x64
& "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe" `
    interceptors\peripheral_storage\ShellExtension\DlpShellExt.vcxproj /p:Configuration=Debug /p:Platform=x64
# then mirror dist\DLP\ structure (Python copies via robocopy or shutil-equivalent in PS)
```
These commands are syntactically verified against existing project files in the repo; **I have not executed them in this plan-mode session.** Confirm by running once before relying on `--install`.

## Verification

### Build / dependency commands (syntactically verified, not yet executed)

From **Visual Studio 2026 Developer PowerShell** at repo root (for the artifact prep — Q1 says these are NOT part of `--install` itself):
```powershell
dotnet build src\ClipboardInterceptor\ClipboardInterceptor.csproj
dotnet build interceptors\peripheral_storage\Controller\Controller.csproj
dotnet build interceptors\peripheral_storage\TransferAgent\DlpTransferAgent.csproj
& "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe" interceptors\peripheral_storage\Payload\Payload.vcxproj /p:Configuration=Debug /p:Platform=x64
& "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe" interceptors\peripheral_storage\ShellExtension\DlpShellExt.vcxproj /p:Configuration=Debug /p:Platform=x64
```

From **normal PowerShell** with `.venv` active (developer-side; not the installed flow):
```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest scripts\harness\ -v
# expect: existing 10 tests (9 Phase B + 1 Phase C lifecycle) PLUS the new test_installer cases
```

From **elevated Developer PowerShell** (the actual install + uninstall — to be run on a clean Win11 VM ideally, or your dev box after `--uninstall` runs cleanly first):
```powershell
# install (run once)
python -m orchestrator --install --config config.yaml

# verify
sc query DLPAgent
reg query "HKLM\Software\Classes\CLSID\{B3A1C2D4-E5F6-7890-ABCD-EF1234567890}\InProcServer32"
reg query "HKLM\SOFTWARE\DLPAgent" /v TransferAgentPath
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer
Get-Content "$env:ProgramData\DLP\state\installed_ca.txt"
certutil -store Root | findstr mitmproxy

# placeholder service smoke (should start and idle)
sc start DLPAgent
sc query DLPAgent
sc stop DLPAgent

# uninstall
python -m orchestrator --uninstall

# verify all reversed
sc query DLPAgent              # expect: SERVICE_DOES_NOT_EXIST
reg query "HKLM\Software\Classes\CLSID\{B3A1C2D4-E5F6-7890-ABCD-EF1234567890}"   # expect: not found
certutil -store Root | findstr mitmproxy                                           # expect: empty
```

### End-to-end smoke (manual, on a Win11 26200 VM ideally)

Run in order; each step is a Done-when checkpoint.

1. **Pre-state baseline.** All registry queries above return "not found"; `sc query DLPAgent` says SERVICE_DOES_NOT_EXIST.
2. **Stage payload.** Run `scripts\prepare-install-payload.ps1` (developer side). Verify `dist\DLP\bin\Controller\UsbDlpController.exe`, `dist\DLP\bin\TransferAgent\DlpTransferAgent.exe`, etc. exist. *Done when:* every artifact in §IT-D3 step 3 resolves to a file.
3. **Install.** Elevated `python -m orchestrator --install --config dist\DLP\config.yaml`. Exit code 0. *Done when:* all the `reg query` / `sc query` / `certutil` commands above return their expected non-empty results.
4. **Context menu round-trip.** Restart Explorer (`taskkill /f /im explorer.exe & start explorer`). Right-click any file → "Transfer to USB (DLP Protected)" appears. Click it; `TransferForm` window opens. Run `python -m orchestrator --foreground` in another shell (admin Developer PS); pick a clean test file → ALLOWED + transferred; pick a file with a Vietnamese CCCD → BLOCKED with policy-derived note. *Done when:* both decisions appear correctly in the TransferForm and corresponding `dlp-agent.log` lines say `ALLOW` / `BLOCK`.
5. **Service placeholder smoke.** `sc start DLPAgent`; `sc query DLPAgent` shows RUNNING; event log shows the WARNING line "PLACEHOLDER: run `--foreground`"; `sc stop DLPAgent` returns it to STOPPED. *Done when:* state transitions complete with no errors.
6. **Uninstall.** `python -m orchestrator --uninstall`. Exit 0. *Done when:* every verification command from step 3 returns the pre-state baseline of step 1.
7. **Uninstall idempotency.** Run `--uninstall` again. Exit 0; log shows "already absent" INFO lines for every step. *Done when:* no errors and no spurious failures.
8. **Phase A/B/C regressions.** With `python -m orchestrator --foreground` from the **source tree** (not installed): the existing 10 pytests + 2 AgentCore test groups still pass. Full Phase C smoke (cold spawn, mitmdump crash respawn, Ctrl+C clean shutdown) still passes.

## Risks

**R1 — pywin32 with embeddable Python.** The embeddable distribution doesn't run pywin32's post-install script. Without it, `import pywintypes` either fails to find `pywintypes313.dll` or finds the wrong one. *Mitigation:* `prepare-python-embed.ps1` patches `python313._pth` to enable `Lib\site-packages` and writes a one-line `sitecustomize.py` containing `import os, pathlib; os.add_dll_directory(str(pathlib.Path(__file__).parent / "Lib" / "site-packages" / "pywin32_system32"))`. Validation: on the install VM, `<install_root>\python\python.exe -c "import win32service, win32event, servicemanager; print('ok')"` must succeed *before* anything else. If it doesn't, the prep script is broken — fix prep, not the installer.

**R2 — ShellExt DLL pinned in `explorer.exe`.** Once Explorer renders the menu once, Windows refuses to delete `DlpShellExt.dll`. *Mitigation:* uninstall step 10's undo deregisters the keys + sends `SHChangeNotify(SHCNE_ASSOCCHANGED)` **before** step 5's undo (`rmtree(install_root)`). The rmtree wraps DLL deletion in a 3-attempt 200 ms retry loop; on persistent failure it schedules the path via `MoveFileExW(path, NULL, MOVEFILE_DELAY_UNTIL_REBOOT)` and logs WARN. Uninstall still returns 0 — the file is gone at next reboot. (`MoveFileExW` `MOVEFILE_DELAY_UNTIL_REBOOT` flag verified against [Microsoft Learn: MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw).)

**R3 — Service registered but does no DLP work (Q3 placeholder).** Operator does `sc start DLPAgent`, sees "RUNNING", expects DLP enforcement. *Mitigation:* the service description string + the WARNING event-log line on every `SvcDoRun` both say "PLACEHOLDER; run `--foreground` for actual operation"; `dlp-agent.log` records the same. Once-per-machine confusion at most — operators reading the log will see it immediately.

**R4 — x64-only.** Native binaries (Payload.dll, DlpShellExt.dll) are x64-only by .vcxproj. *Mitigation:* step 2 (`check_arch`) aborts on non-AMD64 with a clear message naming the missing piece. Cheap and explicit.

**R5 — Manifest desync.** Operator manually deletes `state_dir` (or never had it because a previous install crashed before the first step persisted), then runs `--uninstall`. *Mitigation:* `run_uninstall` synthesizes a default manifest from `config.yaml`. Registry keys are deterministic; CA thumbprint is the only data we can't reconstruct — for that we log a WARN with explicit `certmgr.msc` cleanup pointer. Net result: uninstall completes; CA cleanup is operator-visible.

**R6 — CA bootstrap port conflict despite `--no-server`.** If mitmproxy's `--no-server` regresses across versions, the bootstrap could try to bind 8080 and fail (or worse, hijack a real process's port for ~1 second). *Mitigation:* always pass `--no-server`; if cert generation fails for any reason, capture stderr, abort the install. The whole bootstrap window is sub-second; collision unlikely.

**R7 — Concurrent dev + installed orchestrators racing for named pipes.** Developer runs `python -m orchestrator --foreground` from the source tree while `DLPAgent` service is also RUNNING; both try to bind `\\.\pipe\dlp_agent_data` and `\\.\pipe\dlp_agent_ctl`. Whichever loses the race exits with a pipe error. *Mitigation:* `server.py` startup logs a WARN if `sc query DLPAgent` (best-effort via subprocess; missing sc.exe is fine) reports RUNNING. We don't fail-stop because dev convenience matters; the warning makes the situation diagnosable from the log alone.

## Verification I have not yet performed

These are checks I want to do before/during implementation but can't in plan mode:
1. **Live test:** does `<python_embed>\python.exe -m mitmproxy.tools.dump --no-server --set confdir=<tmp>` actually create `mitmproxy-ca-cert.cer` within 10 s on the prep'd embed? — must validate on the dev box before relying on it in step 6.
2. **Live test:** the exact `sc create` argv list with proper quoting of the embed's path under `binPath=` — Windows argument parsing for sc.exe is famously fragile.
3. **Live test:** is `python313._pth` enough to make `import win32service` work, or do we also need to drop pywin32 DLLs adjacent to `python.exe`? Two viable options; pick the one that actually works on the prep'd embed (R1).
4. The exact set of files in each .NET 10 publish dir (depends on the Visual Studio 2026 publish profile and any global.json present) — `prepare-install-payload.ps1` should `Get-ChildItem` them and decide whether to copy the whole dir or filter.

These are flagged here so they don't get forgotten during implementation; none of them invalidate the plan, but each is a place where the implementation may need a small tweak.

---

## Post-implementation fix #1 — DlpShellExt build output path

### Symptom

After IT-D8 landed, running `scripts\prepare-install-payload.ps1` (from either Developer PowerShell or normal PowerShell) failed at the artifact-verification step with:

```
The following artifacts were expected but not produced:
  D:\Code\GithubPublishEndpointDLP\interceptors\peripheral_storage\out\ShellExtension\Debug\DlpShellExt.dll
Exception: D:\Code\GithubPublishEndpointDLP\scripts\prepare-install-payload.ps1:69
Artifact verification failed.
```

Every other artifact (ClipboardInterceptor.exe, UsbDlpController.exe, DlpTransferAgent.exe, Payload.dll) was produced at the expected path. Only `DlpShellExt.dll` was missing.

### Root cause

`interceptors\peripheral_storage\ShellExtension\DlpShellExt.vcxproj:52` declares:

```xml
<OutDir>$(SolutionDir)out\ShellExtension\$(Configuration)\</OutDir>
```

— output is parameterized by `$(SolutionDir)`. When MSBuild is invoked on a standalone `.vcxproj` **without** `/p:SolutionDir=...`, MSBuild defaults `$(SolutionDir)` to the directory containing the `.vcxproj` itself. So the DLL landed at:

```
interceptors\peripheral_storage\ShellExtension\out\ShellExtension\Debug\DlpShellExt.dll
```

(confirmed by Glob — the DLL exists, just one directory level too deep), instead of the expected:

```
interceptors\peripheral_storage\out\ShellExtension\Debug\DlpShellExt.dll
```

The legacy `interceptors\peripheral_storage\verify-install.ps1` (now a tombstone) passed `/p:SolutionDir=$ScriptDir\` where `$ScriptDir` was the script's own directory (= `interceptors\peripheral_storage`). My new `prepare-install-payload.ps1` dropped that flag, so the DLL went to the project-local default instead.

Payload.vcxproj doesn't reference `$(SolutionDir)` — its OutDir uses the C++ default (`$(ProjectDir)$(Platform)\$(Configuration)\`), so Payload landed at `interceptors\peripheral_storage\Payload\x64\Debug\Payload.dll` as expected. This is why only DlpShellExt failed.

### Fix

`scripts\prepare-install-payload.ps1` — split the single C++ build loop into per-project blocks so DlpShellExt can pass `/p:SolutionDir=<interceptors\peripheral_storage>\`. The trailing backslash is mandatory: MSBuild treats `$(SolutionDir)` as a directory prefix that gets concatenated with `out\…`, and without the trailing slash the resulting path is malformed (e.g. `peripheral_storageout\…`).

Shape of the replacement (the existing `$CppProjects` foreach is replaced by these two blocks):

```powershell
# Payload.vcxproj — no SolutionDir needed (uses default project-local OutDir).
$PayloadProj = Join-Path $RepoRoot "interceptors\peripheral_storage\Payload\Payload.vcxproj"
Write-Host "msbuild Payload" -ForegroundColor Yellow
& $MSBuild $PayloadProj "/p:Configuration=$Configuration" "/p:Platform=x64"
if ($LASTEXITCODE -ne 0) { throw "msbuild Payload failed (exit=$LASTEXITCODE)" }

# DlpShellExt.vcxproj — OutDir uses $(SolutionDir). Pass it explicitly so the
# DLL lands at interceptors\peripheral_storage\out\ShellExtension\<Config>\,
# matching the legacy verify-install.ps1 convention and the config.yaml
# default shell_extension_dll path. Trailing backslash is REQUIRED — MSBuild
# concatenates $(SolutionDir) with "out\..." with no separator.
$ShellExtSolutionDir = (Join-Path $RepoRoot "interceptors\peripheral_storage") + "\"
$ShellExtProj = Join-Path $RepoRoot "interceptors\peripheral_storage\ShellExtension\DlpShellExt.vcxproj"
Write-Host "msbuild DlpShellExt (SolutionDir=$ShellExtSolutionDir)" -ForegroundColor Yellow
& $MSBuild $ShellExtProj "/p:Configuration=$Configuration" "/p:Platform=x64" "/p:SolutionDir=$ShellExtSolutionDir"
if ($LASTEXITCODE -ne 0) { throw "msbuild DlpShellExt failed (exit=$LASTEXITCODE)" }
```

The `$CppProjects = @(...)` array + its `foreach` loop (the current lines that wrap the two `msbuild` calls) get removed.

### Cleanup of stale build artifact (optional but recommended)

The failed run left `interceptors\peripheral_storage\ShellExtension\out\ShellExtension\Debug\DlpShellExt.dll` on disk at the wrong path. It doesn't break anything — the installer reads `cfg.shell_extension_dll` which points at the *correct* path — but the stale copy is misleading. Delete it after the fix lands:

```powershell
Remove-Item -Recurse -Force "interceptors\peripheral_storage\ShellExtension\out"
```

(The Release-config equivalent `interceptors\peripheral_storage\ShellExtension\out\ShellExtension\Release\DlpShellExt.dll` does NOT exist; only Debug was affected because the user only built Debug.)

### Verification

After applying the fix, re-run from any PowerShell at repo root:

```powershell
.\scripts\prepare-install-payload.ps1
```

*Done when:* the script prints `All artifacts built and verified.` and exits 0. The presence of the DLL at the expected path can also be confirmed via:

```powershell
Test-Path "interceptors\peripheral_storage\out\ShellExtension\Debug\DlpShellExt.dll"
# expect: True
```

Existing pytest regression suite is unaffected (no Python code changes); skipping a re-run is acceptable. Direct verification that the installer can find the artifact:

```powershell
.\.venv\Scripts\python.exe -c "from pathlib import Path; from orchestrator.config import load_config; c = load_config(); p = Path('D:/Code/GithubPublishEndpointDLP') / c.shell_extension_dll; print(p, '->', p.is_file())"
```

*Done when:* prints the absolute path followed by `-> True`.

### Risks

**RZ1 — Trailing-backslash quoting on native command parsing.** PowerShell 7+ (the version that ships with VS 2026 Developer PS and the Windows 11 default) parses `"/p:SolutionDir=$path\"` correctly — the trailing backslash inside the double-quoted argument does not escape the quote. PowerShell 5.x had edge cases here, but is not in scope per the project's tooling matrix. If a future tooling change re-introduces 5.x, switch to forward slashes (`/p:SolutionDir=C:/path/` — MSBuild accepts them) as a defensive alternative.

**RZ2 — Diverges from the deprecated `verify-install.ps1` only by being correct.** The legacy script used the same `/p:SolutionDir=$ScriptDir\` pattern; this fix restores parity, so anyone reading the tombstone for context sees the same MSBuild invocation shape. No behavioral surprise.

---

## Post-implementation fix #2 — bootstrap_ca: drop `--no-server`, use ephemeral port

### Symptom

After fix #1 landed and the user successfully built artifacts + ran `python -m orchestrator --install --config config.yaml` from an elevated shell, the install failed at the `bootstrap_ca` step with:

```
2026-06-02 17:17:50,444 INFO   bootstrap_ca: launching C:\Program Files\DLP\python\python.exe -m mitmproxy.tools.dump --no-server --set confdir=C:\ProgramData\DLP\mitmproxy
2026-06-02 17:17:51,685 ERROR  install: step 'bootstrap_ca' failed; rolling back
RuntimeError: mitmdump exited (code=0) without producing the CA cert.
stdout:
stderr:
```

mitmdump ran for ~1.2 s, exited cleanly with code 0, produced no stdout/stderr, and did not write `mitmproxy-ca-cert.cer` (or any other file) into the confdir. The rollback ran successfully (every prior step's undo fired), so the filesystem is back to clean state — the `Program Files\DLP\` tree was rmtree'd; the `ProgramData\DLP\state\install_manifest.json` was deleted.

### Root cause

`--no-server` short-circuits mitmdump's addon initialization in mitmproxy 10+/11. Specifically:

- CA generation lives in the `tls_config` addon's `configure()` hook, where it calls `mitmproxy.certs.CertStore.from_store(confdir, "mitmproxy", 2048, None)`. That call writes `mitmproxy-ca.pem` + `mitmproxy-ca-cert.cer` + `mitmproxy-ca-cert.p12` + `mitmproxy-dhparam.pem` into confdir if missing.
- The `configure()` hook only fires once Master initializes the addon stack as part of normal proxy startup.
- `--no-server` causes Master to exit before the addon stack is brought up (it has nothing to do without a server), so `tls_config.configure()` never runs, so `CertStore.from_store()` never gets called, so no CA files appear.

The plan's R6 risk note already anticipated this exact failure mode: *"If mitmproxy's `--no-server` regresses across versions, the bootstrap could try to bind 8080 and fail (or worse, hijack a real process's port for ~1 second). Mitigation: ... `--listen-port 0` is an acceptable fallback (kernel-assigned port)."* What we actually hit is a more benign variant — mitmdump didn't bind anything wrong, it just no-op'd cleanly.

### Fix

**Original hypothesis (didn't work)**: replace `--no-server` with `--listen-port 0` so mitmdump runs the normal proxy-server path with an ephemeral kernel-assigned port, expecting the addon stack to initialize and generate the CA. Smoke test on `python-embed\python.exe -m mitmproxy.tools.dump --listen-port 0 --set confdir=<tmp>` against mitmproxy 12.2.3 reproduced the same failure mode: mitmdump runs but never calls `tls_config.configure()`. mitmdump's CLI no longer triggers CA generation at all on this version path.

**Actually shipped (verified working)**: bypass mitmdump entirely and invoke `mitmproxy.certs.CertStore.from_store` via a one-shot `python -c` subprocess. This is the documented public API (see [mitmproxy.certs docs](https://docs.mitmproxy.org/stable/api/mitmproxy/certs.html)) and was already pre-described in RW3 as the escape hatch. Verified on mitmproxy 12.2.3 in this session — generates all six expected files synchronously (`mitmproxy-ca.pem`, `mitmproxy-ca-cert.pem`, `mitmproxy-ca-cert.cer`, `mitmproxy-ca-cert.p12`, `mitmproxy-ca.p12`, `mitmproxy-dhparam.pem`) in sub-second elapsed time. No port binding, no event loop, no polling.

`orchestrator\installer.py:_step_bootstrap_ca` — replace the entire `do()` body. The new shape (the `_SCRIPT` constant lives at the closure scope so it's parsed once):

```python
_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "from mitmproxy.certs import CertStore\n"
    "p = Path(sys.argv[1])\n"
    "p.mkdir(parents=True, exist_ok=True)\n"
    "CertStore.from_store(p, 'mitmproxy', 2048, None)\n"
)

def do(ctx):
    cer = ctx.mitm_confdir / "mitmproxy-ca-cert.cer"
    if cer.is_file():
        return {"cer": str(cer), "confdir": str(ctx.mitm_confdir)}
    python_exe = ctx.install_root / "python" / "python.exe"
    if not python_exe.is_file():
        python_exe = ctx.dev_root / "python-embed" / "python.exe"
    result = subprocess.run(
        [str(python_exe), "-c", _SCRIPT, str(ctx.mitm_confdir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            f"CertStore.from_store failed (exit={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}")
    if not cer.is_file():
        raise RuntimeError(
            f"CertStore.from_store completed cleanly but {cer} is missing. "
            "mitmproxy may have changed its CA file naming.")
    return {"cer": str(cer), "confdir": str(ctx.mitm_confdir)}
```

The Popen+polling loop, the 10-second poll deadline, and the terminate/kill finally-block are all deleted — they're no longer needed because `subprocess.run` is synchronous. The undo function is unchanged.

### Pre-cleanup before retrying

The rollback after the failed `bootstrap_ca` was clean (per the user's log), so no manual cleanup is strictly required. But if you want to be paranoid, an explicit `python -m orchestrator --uninstall` is idempotent and confirms a clean slate before re-running `--install`. The two state locations to watch:

- `C:\Program Files\DLP\` — should not exist after rollback. Confirm with `Test-Path "$env:ProgramFiles\DLP"` (expect `False`).
- `C:\ProgramData\DLP\` — may contain empty `logs\`, `state\`, `mitmproxy\` subdirs. Harmless; the next install reuses them.

### Verification

After applying the fix, re-run from elevated Developer PowerShell at repo root:

```powershell
python -m orchestrator --install --config config.yaml
```

*Done when:* exit code 0 and the log contains `bootstrap_ca: cert produced at C:\ProgramData\DLP\mitmproxy\mitmproxy-ca-cert.cer` followed by `install: install_root_ca`, `install: backup_proxy`, etc. through `install: install_service` and the final `Install complete. Run sc start DLPAgent ...` line.

Stand-alone smoke without going through the full installer (useful for iterating on this one step):

```powershell
"C:\Program Files\DLP\python\python.exe" -m mitmproxy.tools.dump --listen-port 0 --set confdir=C:\ProgramData\DLP\mitmproxy
# wait ~2 seconds, then Ctrl+C
Test-Path "C:\ProgramData\DLP\mitmproxy\mitmproxy-ca-cert.cer"   # expect: True
```

*Done when:* `Test-Path` prints `True`. (If you ran this before the fix, also delete the confdir first to force regeneration: `Remove-Item -Recurse -Force "C:\ProgramData\DLP\mitmproxy"`.)

### Risks

**RW1 — Ephemeral port binding is observable to other processes for ~1 second.** A security scanner watching for new listening sockets would see one appear briefly. *Acceptable for the installer context* — install is operator-initiated, not automated; one sub-second listener is well below the noise floor.

**RW2 — mitmdump may print proxy startup banner to stdout/stderr.** Without `--no-server`, mitmdump emits its standard "Proxy server listening at..." banner. Currently we capture stdout/stderr into `subprocess.PIPE` and only surface them on failure, so the banner is silently discarded on success. Harmless; if it ever becomes useful for diagnostics, add an INFO-level log line that echoes the captured output.

**RW3 — `CertStore.from_store` API alternative is available if the CLI path regresses again.** From [mitmproxy.certs docs](https://docs.mitmproxy.org/stable/api/mitmproxy/certs.html), `CertStore.from_store(path, basename, key_size, passphrase)` synchronously generates all CA files (`.pem`, `.cer`, `.p12`, `.dhparam.pem`). If a future mitmproxy version changes mitmdump's startup such that even `--listen-port 0` no longer triggers `tls_config.configure()`, switch to a one-shot `python -c "from mitmproxy.certs import CertStore; CertStore.from_store(...)"` invocation. Noted here so the next maintainer has the escape hatch documented; not implementing today because the CLI path is more stable across the mitmproxy version range we support.

---

## Post-implementation fix #3 — Service can't import `orchestrator` package from SCM-launched binPath

### Symptom

After fix #2, the install completed cleanly. Every Phase D checkpoint up through the registry / cert / proxy steps verified correctly:

```
HKLM\Software\Classes\CLSID\{B3A1C2D4-...}\InProcServer32  → C:\Program Files\DLP\bin\ShellExt\DlpShellExt.dll  ✓
HKLM\SOFTWARE\DLPAgent  TransferAgentPath → C:\Program Files\DLP\bin\TransferAgent\DlpTransferAgent.exe  ✓
HKCU\…\Internet Settings  ProxyServer → 127.0.0.1:8080  ✓
%ProgramData%\DLP\state\installed_ca.txt → FF8206AAB8751FC0CC6EE138863E94679C9EBFCA  ✓
certutil -store Root | findstr mitmproxy  →  two entries (CA cert; the duplicate is harmless and is just certutil printing Issuer + Subject lines per cert)  ✓
```

But the placeholder service smoke fails:

```
PS> sc start DLPAgent
[SC] StartService FAILED 1053:
The service did not respond to the start or control request in a timely fashion.

PS> sc query DLPAgent
SERVICE_NAME: DLPAgent
        TYPE               : 10  WIN32_OWN_PROCESS
        STATE              : 1  STOPPED
        WIN32_EXIT_CODE    : 0  (0x0)
```

The service process exits cleanly (`WIN32_EXIT_CODE: 0`) without ever transitioning to RUNNING. SCM waits the default ~30 s, returns 1053, and reports STOPPED. The subsequent `sc stop` fails with 1062 (ERROR_SERVICE_NOT_ACTIVE) because there's nothing running to stop.

### Root cause

SCM launches services with **cwd = `C:\Windows\System32\`** by default. Our `binPath=` is:

```
"C:\Program Files\DLP\python\python.exe" -m orchestrator --service --config "C:\Program Files\DLP\config.yaml"
```

The embeddable Python ships with a `python313._pth` file. **Per Python docs ([sys.path init](https://docs.python.org/3/library/sys_path_init.html)), when a `_pth` file is present, Python runs in *isolated* mode: only the paths listed in `_pth` are added to `sys.path`. The current working directory is NOT added, even for `-m module` invocations.**

The current `python-embed\python313._pth` (after running `prepare-python-embed.ps1`) contains:

```
python313.zip
.

import site
Lib\site-packages
```

The `.` entry resolves to `<install_root>\python\` (the directory containing `python.exe`). None of the four resulting `sys.path` entries (`<install_root>\python\python313.zip`, `<install_root>\python\`, `<install_root>\python\`, `<install_root>\python\Lib\site-packages`) contains an `orchestrator/` directory — that lives at `<install_root>\orchestrator\`, a sibling of `python\`.

So `python -m orchestrator` fails with `No module named orchestrator` before any of our `__main__.py` code runs. The process dies, SCM never sees `SERVICE_RUNNING`, and 30 seconds later 1053 surfaces.

**Why `--foreground` worked locally:** the dev `.venv\Scripts\python.exe` does NOT have a `_pth` file, so Python's normal sys.path init applies — which adds cwd to `sys.path[0]` for `-m`. Running from the repo root makes `orchestrator/` importable. The same code path silently broke once we switched to the `_pth`-isolated embeddable Python in Phase D.

### Fix

Add `..` to `python313._pth`. The path `..` is interpreted relative to the directory containing `python.exe`, so it resolves to `<install_root>\` — putting the orchestrator/analyzer/interceptors packages on `sys.path` regardless of cwd. Two edits:

**1. `scripts\prepare-python-embed.ps1`** — extend the `_pth` patching block so future preps produce a correct embed. Locate the current block:

```powershell
$pth = $pth -replace '(?m)^#\s*import\s+site\s*$', 'import site'
if ($pth -notmatch '(?m)^Lib\\site-packages\s*$') {
    $pth = $pth.TrimEnd("`r","`n") + "`r`nLib\site-packages`r`n"
}
```

And immediately after the `Lib\site-packages` append, add the `..` append:

```powershell
if ($pth -notmatch '(?m)^\.\.\s*$') {
    $pth = $pth.TrimEnd("`r","`n") + "`r`n..`r`n"
}
```

**2. `python-embed\python313._pth`** — direct edit on the dev tree so the next `--install` redeploys a fixed embed. Append a single line `..` (no trailing comment or whitespace; `_pth` entries must be bare). After the edit the file should read:

```
python313.zip
.

# Uncomment to run site.main() automatically
import site
Lib\site-packages
..
```

After both edits, the user runs `python -m orchestrator --uninstall` (to clear the broken installed copy) and then `python -m orchestrator --install --config config.yaml` (to redeploy with the fixed embed). The `copy_payload` step copies the patched `python-embed\` → `C:\Program Files\DLP\python\`, so the installed `_pth` ends up correct.

### Pre-cleanup before retrying

Two ways to validate the fix:

**Path A (fast — manual one-off, doesn't re-deploy):** edit `C:\Program Files\DLP\python\python313._pth` *directly* (add `..` on a new line at the end). Then `sc start DLPAgent`. If it transitions to RUNNING, the fix is confirmed; *now* do path B for a clean install record.

**Path B (canonical):** apply both edits in the dev tree, then `python -m orchestrator --uninstall` + `python -m orchestrator --install --config config.yaml`. Re-test `sc start DLPAgent`.

### Verification

After applying the fix, from an elevated Developer PowerShell:

```powershell
sc start DLPAgent
sc query DLPAgent          # expect: STATE 4 RUNNING (or 2 START_PENDING then 4 RUNNING)
sc stop DLPAgent
sc query DLPAgent          # expect: STATE 1 STOPPED, exit 0
```

Cross-check via the event log:

```powershell
Get-EventLog -LogName Application -Source DLPAgent -Newest 5
# expect: a WARNING line containing "(Phase D PLACEHOLDER: run `python -m orchestrator --foreground` for actual DLP enforcement)"
```

The `dlp-agent.log` under `%PROGRAMDATA%\DLP\logs\` should also contain a fresh `DLPAgent placeholder started.` line after `sc start` and `DLPAgent placeholder stopped.` after `sc stop`.

A standalone smoke without going through SCM (catches bad `_pth` more quickly during iteration):

```powershell
# From any directory — simulates SCM's System32 cwd
cd C:\Windows\System32
& "C:\Program Files\DLP\python\python.exe" -c "import orchestrator; print(orchestrator.__file__)"
# expect: C:\Program Files\DLP\orchestrator\__init__.py (or similar)
```

*Done when:* the import succeeds and prints the install-root path. A `ModuleNotFoundError` means `..` didn't land in `_pth` — re-check the file.

### Risks

**RV1 — `..` in `_pth` is interpreted relative to `python.exe`, not to the `_pth` file location.** They're the same directory in our case (both at `<install_root>\python\`), so this works. If the `_pth` is ever moved or symlinked, the entry breaks silently. Defensive note: keep `_pth` adjacent to `python.exe`.

**RV2 — Adding `..` puts every top-level dir under `<install_root>\` on `sys.path`.** That includes `orchestrator/`, `analyzer/`, `interceptors/`, `bin/`, `config.yaml`. The first three are legitimate packages; `bin/` and the `config.yaml` file are not Python packages and Python silently ignores them during import resolution. No name collisions with stdlib or installed packages. Safe.

**RV3 — Future Phase E session-aware service may rely on different cwd semantics.** Phase E plans to spawn child processes via `CreateProcessAsUser` into user sessions; those children will need the same `_pth` fix if they reuse the embeddable Python. Mention in Phase E's planning session that this `_pth` line is load-bearing for any SCM-or-CreateProcessAsUser launched `python -m orchestrator …` invocation.

**RV4 — Subsequent re-runs of `prepare-python-embed.ps1` are idempotent thanks to the `if ($pth -notmatch '(?m)^\.\.\s*$')` guard.** No risk of duplicate `..` entries.

---

## Post-implementation fix #4 — Defer analyzer imports per mode

### Symptom

After fix #3 patched `_pth` so `python -m orchestrator` can find the `orchestrator` package from any cwd, `sc start DLPAgent` *still* returns 1053 timeout. `sc query` still shows STOPPED with `WIN32_EXIT_CODE: 0`. Same surface symptom as before fix #3 — but a different underlying cause.

### Root cause (two layers)

**Layer A — eager imports in `__main__.py`.** `orchestrator/__main__.py:19` does `from orchestrator.policy_manager import PolicyManager` at module load time. That cascades:
- `orchestrator/policy_manager.py:12` → `from analyzer.engine import DLPEngine`
- `analyzer/engine.py:26-29` → `import ahocorasick`, `import re2`, `from policy import ACTION_RANK, Policy, load_policies`

These imports run on every `python -m orchestrator <mode>` invocation, even when the mode doesn't need the analyzer (e.g. `--service` placeholder, `--uninstall`).

**Layer B — analyzer deps not in the embed.** `prepare-python-embed.ps1` only runs `pip install -r requirements.txt`, where `requirements.txt` at the repo root only lists `mitmproxy`, `pywin32`, `pyyaml`, `watchdog`. The analyzer's heavy deps (`pyahocorasick`, `google-re2`, `python-docx`, `openpyxl`, `python-pptx`, `odfpy`, `PyMuPDF`, `pymupdf-layout`) live in `analyzer\requirements.txt` and were never installed in `python-embed`. Verified via Glob: `python-embed\Lib\site-packages\ahocorasick*` returns no files.

Combined consequence: `python.exe -m orchestrator --service` fails immediately with `ModuleNotFoundError: No module named 'ahocorasick'` during the __main__.py import block. The service process dies before pywin32's `StartServiceCtrlDispatcher` runs. SCM never sees `SERVICE_RUNNING`, times out at 30 s, surfaces 1053.

The user's `python -m orchestrator --install` from the dev shell worked because the dev `.venv` has the analyzer deps. The first invocation that actually exercises the bundled embed's Python is the SCM-launched service — and that's where the missing deps surface.

### Diagnostic confirmation

The user can verify with one command from any PowerShell:

```powershell
& "C:\Program Files\DLP\python\python.exe" -c "import ahocorasick; print('OK')"
```

*Pre-fix expected:* `ModuleNotFoundError: No module named 'ahocorasick'` (or `'_ahocorasick'`).

If this errors as expected, both layers are confirmed.

### Fix (layer A only; layer B is out of Phase D scope)

Restructure `orchestrator/__main__.py` so heavy imports are **lazy and per-mode**. Each mode imports only what it needs:

| Mode | Imports needed at dispatch time |
|------|----------------------------------|
| `--service` | `orchestrator.service` (pywin32 + logging_setup — all in embed) |
| `--install` / `--uninstall` | `orchestrator.installer` (subprocess + winreg + mitmproxy.certs via subprocess — all in embed) |
| `--foreground` | full set: `config`, `config_watcher`, `ctl_server`, `dispatcher`, `policy_manager`, `server`, `supervisor` (the `policy_manager` chain needs analyzer deps which are NOT in the embed — see layer B below) |

Concrete shape:

```python
import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "analyzer"))  # engine.py uses bare `from policy import`

# Top-level: nothing from orchestrator.* that pulls analyzer deps.
# Mode-specific imports happen inside each dispatch branch below — keeps
# `python -m orchestrator --service` startup well under SCM's 30 s timeout
# even when the embed doesn't have ahocorasick / re2 / presidio installed.


def main() -> None:
    parser = argparse.ArgumentParser("python -m orchestrator")
    parser.add_argument("--config", type=Path, default=None,
                        help="Path to config.yaml (defaults to repo root).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--foreground", action="store_true")
    group.add_argument("--install",    action="store_true")
    group.add_argument("--uninstall",  action="store_true")
    group.add_argument("--service",    action="store_true")
    args = parser.parse_args()

    if args.foreground:
        _run_foreground(args.config)
    elif args.install:
        from orchestrator.installer import run_install
        sys.exit(run_install(args.config))
    elif args.uninstall:
        from orchestrator.installer import run_uninstall
        sys.exit(run_uninstall(args.config))
    elif args.service:
        from orchestrator.service import run_as_service
        run_as_service()
    else:
        parser.error("no mode selected; …")


def _maybe_install_slow_test_hook() -> None:
    raw = os.environ.get("DLP_TEST_SLOW_MS")
    if not raw:
        return
    try:
        delay_s = float(raw) / 1000.0
    except ValueError:
        return
    # Lazy import — PolicyManager pulls analyzer deps that may not be installed.
    from orchestrator.policy_manager import PolicyManager
    original_analyze = PolicyManager.analyze
    # … rest unchanged


def _run_foreground(config_path: Path | None = None) -> None:
    # Heavy imports stay here so --service / --install / --uninstall don't trigger them.
    from orchestrator.config import load_config
    from orchestrator.config_watcher import ConfigWatcher
    from orchestrator.ctl_server import CtlServer
    from orchestrator.dispatcher import Dispatcher
    from orchestrator.logging_setup import configure_logging
    from orchestrator.policy_manager import PolicyManager
    from orchestrator.server import PipeServer
    from orchestrator.supervisor import Supervisor, build_default_specs

    configure_logging(foreground=True)
    # … rest of _run_foreground unchanged
```

The harness pytests under `scripts/harness/` import orchestrator submodules directly (e.g., `from orchestrator.config import OrchestratorConfig`), not via `__main__.py`. They keep working unchanged.

### Layer B (out of Phase D scope, but documented)

For `--foreground` to work from the *installed* `%ProgramFiles%\DLP\`, the embed needs the analyzer's deps too. That's a separate change:

- Add a `pip install -r analyzer\requirements.txt` step to `scripts\prepare-python-embed.ps1` (after the existing top-level requirements install).
- Re-run prep + re-install.

This is deferred because:
1. The Phase D "done-when" only requires `sc query DLPAgent` working post-install (the placeholder service), which fix #4 layer A delivers.
2. Bundling the analyzer adds substantial weight to the embed (PyMuPDF + python-docx + openpyxl + odfpy + presidio-equivalent — easily 200+ MB).
3. Operators currently run `python -m orchestrator --foreground` from the source tree with the dev `.venv` for actual DLP enforcement; the installed service is a placeholder until Phase E.

Phase E's plan will need to call out layer B explicitly so the LocalSystem service body can actually run analysis.

### Verification

1. **Diagnostic confirmation (pre-fix):**
   ```powershell
   & "C:\Program Files\DLP\python\python.exe" -c "import ahocorasick"
   # expect: ModuleNotFoundError
   ```

2. **Apply the fix** to `orchestrator/__main__.py`. **Re-deploy** so the installed `<install_root>\orchestrator\__main__.py` picks up the change:
   ```powershell
   python -m orchestrator --uninstall
   python -m orchestrator --install --config config.yaml
   ```

3. **Smoke that import path is light (post-fix):**
   ```powershell
   Push-Location C:\Windows\System32
   & "C:\Program Files\DLP\python\python.exe" -c "import orchestrator.__main__; print('main module loaded')"
   Pop-Location
   # expect: 'main module loaded' (no ModuleNotFoundError)
   ```

4. **Service smoke:**
   ```powershell
   sc start DLPAgent      # expect: SUCCESS
   sc query DLPAgent      # expect: STATE 4 RUNNING
   sc stop DLPAgent       # expect: SUCCESS
   sc query DLPAgent      # expect: STATE 1 STOPPED, exit 0
   ```

5. **Event log corroboration:**
   ```powershell
   Get-EventLog -LogName Application -Newest 10 | Where-Object {$_.Source -match 'DLP|Python'}
   # expect: a WARNING line containing the placeholder banner
   ```

6. **Harness pytest still passes** (Phase A/B/C/D regressions):
   ```powershell
   .\.venv\Scripts\python.exe -m pytest scripts\harness\ -v
   # expect: 18 passed
   ```

7. **--foreground still works from dev tree:**
   ```powershell
   .\.venv\Scripts\python.exe -m orchestrator --foreground
   # expect: same Phase C smoke (cold spawn, Ctrl+C clean) — unchanged behavior
   ```

### Risks

**RU1 — Lazy imports add latency to first invocation of each mode.** `--foreground` now does its heavy imports inside the function body, adding ~10 s to its startup. *Mitigation:* none — operators typically launch `--foreground` once per session; the slowdown is acceptable and matches normal Python REPL behavior for analyzer/presidio-style stacks.

**RU2 — Future code that introduces a NEW eager-import chain pulling analyzer deps will silently re-break the service.** *Mitigation:* the lazy-import block at top of `_run_foreground` carries a comment explaining the constraint. Any reviewer adding `from orchestrator.policy_manager import ...` at module top should be flagged in code review. Adding a smoke check `python.exe -m orchestrator --version` (where `--version` does nothing but verifies imports) to the prep script's validation block would also catch this proactively; consider for Phase E.

**RU3 — Phase E LocalSystem service WILL need the analyzer.** The Phase E plan needs to (a) call out layer B above, (b) likely bundle analyzer deps + presidio + stanza, and (c) re-evaluate the embed size budget. Cross-reference to this fix in Phase E's planning notes.

**RU4 — The fix doesn't address `--foreground` from the installed location.** Operators running `python.exe -m orchestrator --foreground` from the installed `<install_root>\python\` will hit `ModuleNotFoundError: ahocorasick`. *Mitigation:* documented as a known limitation in the Phase D done-when. Phase E layer-B work resolves this.

---

## References (verified during planning)

- [mitmproxy: About Certificates](https://docs.mitmproxy.org/stable/concepts/certificates/) — confirms `--set confdir` + first-run CA generation behavior.
- [Microsoft Learn: certutil](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/certutil) — confirms `-addstore -f Root <cer>` and `-delstore Root <thumbprint>` syntax + exit-code semantics.
- [Microsoft Learn: sc create](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/sc-create) — confirms `binPath=` quoting + the required space after `=`.
- [pywin32 win32serviceutil.py (main)](https://github.com/mhammond/pywin32/blob/main/win32/Lib/win32serviceutil.py) — confirms `HandleCommandLine` semantics and the `PrepareToHostSingle` SCM-dispatch pattern.
- [Python docs: embeddable package on Windows](https://docs.python.org/3/using/windows.html#the-embeddable-package) — confirms the `._pth` file format + the `import site` enabling step.
- [Microsoft Learn: MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw) — confirms `MOVEFILE_DELAY_UNTIL_REBOOT` flag for R2's deferred-delete fallback.
