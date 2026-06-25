"""Phase D --install / --uninstall driver.

Transactional design: each install step is a (do, undo) pair. ``run_install``
runs every ``do`` forward; on any exception, runs ``undo`` for already-completed
steps in reverse. ``run_uninstall`` runs every step's ``undo`` in reverse,
swallowing "already absent" errors so a partial install can always be cleaned.

Per-step success is persisted to ``<state_dir>/install_manifest.json`` after
every successful step, so a crash between steps still leaves enough data to
uninstall.

Phase D scope: machine install only. The Windows service body itself is a
placeholder (see ``orchestrator/service.py``); Phase E fills it in.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from orchestrator.config import OrchestratorConfig, load_config

log = logging.getLogger("orchestrator.installer")

# CLSID + friendly name must stay in sync with the ShellExt source at
# interceptors\peripheral_storage\ShellExtension\DlpContextMenu.h:9-13.
_SHELLEXT_CLSID = "{B3A1C2D4-E5F6-7890-ABCD-EF1234567890}"
_SHELLEXT_FRIENDLY = "DLP File Transfer"
_SHELLEXT_HANDLER_NAME = "DLPTransfer"

_PROXY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# SHChangeNotify constants
_SHCNE_ASSOCCHANGED = 0x08000000
_SHCNF_IDLIST = 0

# MoveFileExW flag for delete-on-reboot fallback (R2 in the plan)
_MOVEFILE_DELAY_UNTIL_REBOOT = 0x4

# PF#6: dlp-ctl convenience wrapper + machine PATH so `dlp-ctl` runs from any
# shell using the bundled embed Python (which has pywin32). The machine Path
# lives in this HKLM key (REG_EXPAND_SZ); changes are broadcast via
# WM_SETTINGCHANGE so new shells pick them up.
_ENV_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
_HWND_BROADCAST = 0xFFFF
_WM_SETTINGCHANGE = 0x1A
_SMTO_ABORTIFHUNG = 0x0002
_CTL_WRAPPER_NAME = "dlp-ctl.cmd"
_CTL_WRAPPER_BODY = (
    "@echo off\n"
    "REM DLP admin CLI. Uses the bundled Python (which has pywin32) so dlp-ctl\n"
    "REM works without any system Python. Run elevated for status / reload.\n"
    '"%~dp0python\\python.exe" -m orchestrator.ctl --config "%~dp0config.yaml" %*\n'
    "exit /b %ERRORLEVEL%\n"
)

# AC-5 follow-up: an installed uninstaller dropped at <install_root>\uninstall.cmd
# so the agent can be removed even after the deploy bundle is gone. It runs the
# INSTALLED python (<install_root>\python\python.exe) — which the App Control
# self-protect FilePath rule (<install_root>\*) allows even while an enforcement
# policy is deployed; the bundle's embed python is NOT covered and WDAC blocks it.
# It self-relaunches from %TEMP% so the running script is never inside the tree it
# deletes (no "batch file cannot be found"), and sets cwd to %SystemRoot% (outside
# the tree) so the tree is removable. The install root is baked in at write time
# (%~dp0 would point at %TEMP% after the relaunch). Module resolution rides the
# embed's python313._pth `..` entry, which puts <install_root> on sys.path.
_UNINSTALL_WRAPPER_NAME = "uninstall.cmd"
_UNINSTALL_WRAPPER_BODY = (
    "@echo off\n"
    "REM DLP Agent uninstaller (installed copy). Run as administrator.\n"
    "REM Uses the INSTALLED python (allowed by the App Control self-protect policy),\n"
    "REM so uninstall works even while an enforcement policy is deployed. Re-launches\n"
    "REM from %TEMP% so it never deletes the script that is running.\n"
    "setlocal\n"
    'if /i "%~1"=="_fromtemp" goto work\n'
    'copy /y "%~f0" "%TEMP%\\dlp-uninstall.cmd" >nul 2>&1\n'
    'start "DLP Uninstall" "%TEMP%\\dlp-uninstall.cmd" _fromtemp\n'
    "exit /b 0\n"
    ":work\n"
    'cd /d "%SystemRoot%"\n'
    '"{install_root}\\python\\python.exe" -m orchestrator --uninstall '
    '--config "{install_root}\\config.yaml"\n'
    "echo.\n"
    "echo DLP uninstall finished.\n"
    "pause\n"
    '(goto) 2>nul & del "%~f0"\n'
)

# Well-known sc.exe / certutil exit codes we treat as "already absent" success
_ERROR_SERVICE_DOES_NOT_EXIST = 1060
_ERROR_SERVICE_NOT_ACTIVE = 1062
_ERROR_SERVICE_EXISTS = 1073
_ERROR_SERVICE_ALREADY_RUNNING = 1056

# AC-5: DISM exit codes we treat as success when enabling ConfigCI offline.
# 0 = ERROR_SUCCESS; 3010 = ERROR_SUCCESS_REBOOT_REQUIRED (installed, reboot
# pending — with /norestart AC-1 saw no reboot on Home 26200, but accept it
# defensively). Any other code is a hard failure (fail-closed, decision D1).
_DISM_SUCCESS_CODES = (0, 3010)

# Actionable hint shown when ConfigCI cannot be enabled (mirrors
# app_control.hashing._DISM_HINT; installer is a different layer, so the string is
# duplicated rather than importing a private name).
_DISM_HINT = (
    r"Enable it offline (admin): "
    r"gci $Env:SystemRoot\servicing\Packages\*ConfigCI*.mum | "
    r'% { dism /online /norestart /add-package:"$($_.FullName)" }'
)


# ─── Dataclasses ────────────────────────────────────────────────────────────


@dataclass
class InstallContext:
    """Resolved + verified install state. Built once per run_install/uninstall."""
    config: OrchestratorConfig
    config_path: Path
    dev_root: Path                          # source tree containing pre-built artifacts
    install_root: Path                      # destination, e.g. %ProgramFiles%\DLP
    state_dir: Path                         # %ProgramData%\DLP\state
    log_dir: Path                           # %ProgramData%\DLP\logs
    mitm_confdir: Path                      # %ProgramData%\DLP\mitmproxy
    service_name: str
    service_display: str
    service_desc: str
    service_start_type: str = "auto"        # Phase F (Q-E4): sc.exe start= value
    artifacts: dict[str, Path] = field(default_factory=dict)
    manifest: list[dict] = field(default_factory=list)

    @property
    def manifest_path(self) -> Path:
        return self.state_dir / "install_manifest.json"


@dataclass
class Step:
    """One install step. ``do`` returns a JSON-serializable undo payload that
    gets persisted to the manifest and passed back to ``undo`` later. Both
    must be safe to call repeatedly — uninstall replays ``undo`` regardless
    of whether ``do`` ran in this session."""
    id: str
    do:   Callable[[InstallContext], dict[str, Any] | None]
    undo: Callable[[InstallContext, dict[str, Any] | None], None]


# ─── Context construction ───────────────────────────────────────────────────


def _resolve_under(dev_root: Path, rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else (dev_root / p).resolve()


def _build_context(config_path: Path | None) -> InstallContext:
    cfg = load_config(config_path)
    if config_path is not None:
        dev_root = Path(config_path).resolve().parent
    else:
        dev_root = Path(__file__).resolve().parent.parent
    inst = (cfg.raw.get("install") or {})

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")

    install_root = Path(inst.get("install_root") or f"{program_files}\\DLP")
    state_dir = Path(inst.get("state_dir") or f"{program_data}\\DLP\\state")
    log_dir = Path(cfg.log_dir or f"{program_data}\\DLP\\logs")
    mitm_confdir = Path(inst.get("mitmproxy_confdir") or f"{program_data}\\DLP\\mitmproxy")

    return InstallContext(
        config=cfg,
        config_path=Path(config_path) if config_path else dev_root / "config.yaml",
        dev_root=dev_root,
        install_root=install_root,
        state_dir=state_dir,
        log_dir=log_dir,
        mitm_confdir=mitm_confdir,
        service_name=inst.get("service_name", "DLPAgent"),
        service_start_type=inst.get("service_start_type", "auto"),
        service_display=inst.get("service_display_name", "DLP Endpoint Agent"),
        service_desc=inst.get(
            "service_description",
            "Endpoint DLP orchestrator (Phase D placeholder; "
            "full session-aware behavior arrives in Phase E)."),
    )


def _persist_manifest(ctx: InstallContext) -> None:
    """Atomic write to ``ctx.manifest_path``."""
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = ctx.manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ctx.manifest, indent=2), encoding="utf-8")
    os.replace(tmp, ctx.manifest_path)


def _load_manifest(state_dir: Path) -> list[dict] | None:
    try:
        return json.loads((state_dir / "install_manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ─── Driver ─────────────────────────────────────────────────────────────────


def _drive_install(ctx: InstallContext, steps: list[Step]) -> int:
    """Run every ``do`` forward; on any exception, roll back completed undos in
    reverse and return 1. SystemExit (admin/arch checks) propagates unchanged."""
    by_id = {s.id: s for s in steps}
    current: Step | None = None
    try:
        for step in steps:
            current = step
            log.info("install: %s", step.id)
            payload = step.do(ctx)
            ctx.manifest.append({"id": step.id, "undo_payload": payload})
            _persist_manifest(ctx)
    except SystemExit:
        raise
    except Exception:
        failed = current.id if current is not None else "<unknown>"
        log.exception("install: step %r failed; rolling back", failed)
        for entry in reversed(ctx.manifest):
            step = by_id.get(entry["id"])
            if step is None:
                continue
            try:
                step.undo(ctx, entry.get("undo_payload"))
            except Exception:
                log.exception("install: undo of %r raised; continuing rollback", entry["id"])
        try:
            ctx.manifest_path.unlink()
        except FileNotFoundError:
            pass
        return 1
    log.info(
        "install: complete. The %s service is set to auto-start and a start was "
        "requested now — verify with `Get-Service %s` (expect Running). If it is "
        "Stopped, start it with `Start-Service %s` (note: bare `sc` in PowerShell "
        "is an alias for Set-Content, not the service controller; use `sc.exe`). "
        "The service spawns the interceptors across user sessions; check "
        "%%ProgramData%%\\DLP\\logs\\dlp-agent.log. Admin CLI: open a NEW shell and "
        "run `dlp-ctl status` (the installer added %s to PATH and dropped "
        "dlp-ctl.cmd there) — or `.\\dlp-ctl.cmd status` from %s.",
        ctx.service_name, ctx.service_name, ctx.service_name,
        ctx.install_root, ctx.install_root)
    return 0


def _drive_uninstall(ctx: InstallContext, steps: list[Step]) -> int:
    """Run every undo in reverse, treating "already absent" errors as success."""
    by_id = {s.id: s for s in steps}
    saved = _load_manifest(ctx.state_dir)
    if saved is None:
        log.info("uninstall: manifest missing; synthesizing default sweep from step list")
        entries = [{"id": s.id, "undo_payload": None} for s in steps]
    else:
        entries = saved

    for entry in reversed(entries):
        step = by_id.get(entry["id"])
        if step is None:
            log.info("uninstall: unknown step %r in manifest; skipping", entry["id"])
            continue
        try:
            step.undo(ctx, entry.get("undo_payload"))
        except (FileNotFoundError, OSError, PermissionError) as e:
            log.info("uninstall: %s already absent or skipped (%s)", entry["id"], e)
        except Exception:
            log.exception("uninstall: %s raised unexpectedly; continuing", entry["id"])

    try:
        ctx.manifest_path.unlink()
    except FileNotFoundError:
        pass
    log.info("uninstall: complete.")
    return 0


# ─── Public entry points (wired from orchestrator/__main__.py) ──────────────


def run_install(config_path: Path | None) -> int:
    from orchestrator.logging_setup import configure_logging
    configure_logging(foreground=True)
    log.info("DLP installer starting")
    ctx = _build_context(config_path)
    return _drive_install(ctx, _build_default_steps())


def run_uninstall(config_path: Path | None) -> int:
    from orchestrator.logging_setup import configure_logging
    configure_logging(foreground=True)
    log.info("DLP uninstaller starting")
    ctx = _build_context(config_path)
    return _drive_uninstall(ctx, _build_default_steps())


# ─── Helpers used by multiple steps ─────────────────────────────────────────


def _noop_undo(_ctx: InstallContext, _payload: dict[str, Any] | None) -> None:
    return None


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.is_dir():
        raise FileNotFoundError(f"copy_tree: source missing: {src}")
    log.info("copy_tree %s -> %s", src, dst)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _rmtree_with_retry(target: Path, attempts: int = 3, delay: float = 0.2) -> None:
    """Delete a directory tree, removing it *now* even when files are locked.

    A mapped image (a loaded DLL / running .exe) cannot be deleted while in use —
    `os.remove` raises ERROR_ACCESS_DENIED (5) — but it CAN be renamed/moved on the
    same volume (the file is opened FILE_SHARE_DELETE; rename needs only DELETE
    access). The locks we hit at uninstall are: Payload.dll (injected into every
    session's explorer.exe), DlpShellExt.dll (shell-ext loaded by explorer), and —
    only if the uninstaller is run from the *installed* interpreter — `python\\*`.

    Strategy: try a plain rmtree (fast path); if it still fails after the retries,
    walk the tree, deleting what we can and **moving each locked file aside** into a
    same-volume pending dir, then removing the now-empty install dirs immediately.
    Only the tiny moved-aside copies are scheduled for delete-on-reboot. A file that
    can't even be moved falls back to in-place reboot-scheduling (no worse than the
    old behavior). No explorer kill (AutoRestartShell isn't guaranteed)."""
    for i in range(attempts):
        try:
            shutil.rmtree(target)
            return
        except FileNotFoundError:
            return
        except (PermissionError, OSError):
            if i + 1 < attempts:
                time.sleep(delay)
                continue
    _rmtree_move_locked_aside(target)


def _rmtree_move_locked_aside(target: Path) -> None:
    # Same-volume pending dir (sibling of target) so moves are renames, not copies.
    pending = target.parent / f".{target.name}.pending-delete"
    try:
        pending.mkdir(parents=True, exist_ok=True)
    except OSError:
        pending = None

    counter = 0
    moved = 0
    unmovable: list[Path] = []
    for root, _dirs, files in os.walk(target, topdown=False):
        root_p = Path(root)
        for name in files:
            f = root_p / name
            try:
                f.unlink()
                continue
            except FileNotFoundError:
                continue
            except (PermissionError, OSError):
                pass  # locked (mapped image) — move it aside
            relocated = False
            if pending is not None:
                dst = pending / f"{counter:04d}_{name}"
                counter += 1
                try:
                    os.replace(f, dst)   # same-volume rename; works on mapped images
                    relocated = True
                except OSError:
                    relocated = False
                if relocated:
                    _schedule_one_delete_on_reboot(dst)
                    moved += 1
            if not relocated:
                _schedule_one_delete_on_reboot(f)
                unmovable.append(f)
        try:
            root_p.rmdir()
        except (FileNotFoundError, OSError):
            pass

    try:
        target.rmdir()
    except (FileNotFoundError, OSError):
        pass
    if pending is not None and pending.exists():
        # Remove the empty pending dir at reboot, after its files (scheduled above).
        _schedule_one_delete_on_reboot(pending)

    if unmovable:
        log.warning(
            "rmtree %s: %d locked file(s) could not be moved aside; scheduled "
            "in-place for delete-on-reboot. A true Restart (not a Fast-Startup "
            "shutdown) is required to finish cleanup.", target, len(unmovable))
    elif moved:
        log.info(
            "rmtree %s: tree removed now; %d locked file(s) moved aside to %s and "
            "scheduled for delete-on-reboot.", target, moved, pending)
    else:
        log.info("rmtree %s: removed.", target)


def _schedule_one_delete_on_reboot(path: Path) -> None:
    move = ctypes.windll.kernel32.MoveFileExW
    move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move.restype = ctypes.c_int
    if not move(str(path), None, _MOVEFILE_DELAY_UNTIL_REBOOT):
        log.info("MoveFileExW(DELAY_UNTIL_REBOOT) failed for %s (err=%s)",
                 path, ctypes.windll.kernel32.GetLastError())


def _schedule_delete_on_reboot(target: Path) -> None:
    if not target.exists():
        return
    move = ctypes.windll.kernel32.MoveFileExW
    move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move.restype = ctypes.c_int
    paths: list[Path] = []
    for root, dirs, files in os.walk(target):
        for name in files:
            paths.append(Path(root) / name)
        for name in dirs:
            paths.append(Path(root) / name)
    paths.append(target)
    paths.sort(key=lambda p: len(p.parts), reverse=True)
    for p in paths:
        if not move(str(p), None, _MOVEFILE_DELAY_UNTIL_REBOOT):
            log.info("MoveFileExW(DELAY_UNTIL_REBOOT) failed for %s (err=%s)",
                     p, ctypes.windll.kernel32.GetLastError())


def _delete_key_recursive(hive: int, path: str) -> None:
    """winreg has no recursive delete; walk children depth-first and unlink."""
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as k:
            info = winreg.QueryInfoKey(k)
            sub_names = [winreg.EnumKey(k, i) for i in range(info[0])]
    except FileNotFoundError:
        return
    for sub in sub_names:
        _delete_key_recursive(hive, path + "\\" + sub)
    try:
        winreg.DeleteKey(hive, path)
    except FileNotFoundError:
        pass


def _cert_thumbprint(cer_path: Path) -> str:
    """SHA-1 fingerprint over the DER-encoded cert. Handles both DER and PEM
    on-disk encodings (mitmproxy uses DER for .cer historically but the format
    has shifted between versions)."""
    data = cer_path.read_bytes()
    if data.lstrip().startswith(b"-----BEGIN CERTIFICATE-----"):
        text = data.decode("ascii", errors="replace")
        b64_lines: list[str] = []
        in_body = False
        for line in text.splitlines():
            if line.startswith("-----BEGIN CERTIFICATE-----"):
                in_body = True
                continue
            if line.startswith("-----END CERTIFICATE-----"):
                break
            if in_body:
                b64_lines.append(line.strip())
        data = base64.b64decode("".join(b64_lines))
    return hashlib.sha1(data).hexdigest().upper()


# ─── Step factory functions ─────────────────────────────────────────────────


def _build_default_steps() -> list[Step]:
    return [
        _step_require_admin(),
        _step_check_arch(),
        _step_verify_artifacts(),
        _step_make_dirs(),
        _step_copy_payload(),
        _step_install_ctl_wrapper(),
        _step_install_uninstall_wrapper(),
        _step_add_to_path(),
        # AC-5 App Control setup, placed early so a fail-closed ConfigCI error (D1)
        # aborts before the heavier CA / proxy / shell-ext / service steps run.
        _step_appcontrol_dirs(),
        _step_enable_configci(),
        _step_bootstrap_ca(),
        _step_install_root_ca(),
        _step_backup_proxy(),
        _step_set_proxy(),
        _step_register_shellext(),
        _step_notify_shell(),
        # AC-5: must be the last step before install_service so its undo runs after
        # the service is stopped and before copy_payload removes the install tree (D3).
        _step_appcontrol_policy_guard(),
        _step_install_service(),
    ]


def _step_require_admin() -> Step:
    def do(_ctx: InstallContext) -> dict[str, Any] | None:
        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_admin = False
        if not is_admin:
            # Distinct exit code so wrapper scripts can branch on "needs elevation"
            # vs "actual install failure".
            print("Re-run from an elevated prompt. --install needs admin for "
                  "HKLM writes, LocalMachine Root cert install, and sc.exe.",
                  file=sys.stderr)
            raise SystemExit(2)
        return None
    return Step("require_admin", do, _noop_undo)


def _step_check_arch() -> Step:
    def do(_ctx: InstallContext) -> dict[str, Any] | None:
        m = platform.machine()
        if m.upper() != "AMD64":
            print(f"DLP installer is x64-only; current arch={m!r}. "
                  "Native binaries (Payload.dll, DlpShellExt.dll) target x64.",
                  file=sys.stderr)
            raise SystemExit(3)
        return None
    return Step("check_arch", do, _noop_undo)


def _step_verify_artifacts() -> Step:
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        cfg = ctx.config
        spec = {
            "controller_exe":      cfg.controller_exe,
            "clipboard_exe":       cfg.clipboard_exe,
            "transfer_agent_exe":  cfg.transfer_agent_exe,
            "shell_extension_dll": cfg.shell_extension_dll,
            "payload_dll":         cfg.payload_dll,
            "addon_script":        cfg.addon_script,
        }
        missing: list[str] = []
        for key, rel in spec.items():
            if not rel:
                missing.append(f"{key}: <empty in config.yaml paths section>")
                continue
            p = _resolve_under(ctx.dev_root, rel)
            if not p.is_file():
                missing.append(f"{key}: {p}")
            else:
                ctx.artifacts[key] = p
        embed_dir = ctx.dev_root / "python-embed"
        embed_exe = embed_dir / "python.exe"
        if not embed_exe.is_file():
            missing.append(f"python-embed/python.exe: {embed_exe}")
        else:
            ctx.artifacts["python_embed_dir"] = embed_dir
        pol = _resolve_under(ctx.dev_root, cfg.policies_file)
        if not pol.is_file():
            missing.append(f"policies_file: {pol}")
        else:
            ctx.artifacts["policies_file"] = pol
        if missing:
            raise FileNotFoundError(
                "Missing pre-built artifacts. Run scripts\\prepare-install-payload.ps1 "
                "and scripts\\prepare-python-embed.ps1 first, then retry --install.\n  "
                + "\n  ".join(missing))
        return None
    return Step("verify_artifacts", do, _noop_undo)


def _step_make_dirs() -> Step:
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        targets = [
            ctx.install_root,
            ctx.install_root / "bin",
            ctx.install_root / "bin" / "Controller",
            ctx.install_root / "bin" / "TransferAgent",
            ctx.install_root / "bin" / "Clipboard",
            ctx.install_root / "bin" / "ShellExt",
            ctx.install_root / "python",
            ctx.state_dir,
            ctx.log_dir,
            ctx.mitm_confdir,
        ]
        created: list[str] = []
        for t in targets:
            if not t.exists():
                t.mkdir(parents=True, exist_ok=True)
                created.append(str(t))
        return {"created": created}

    def undo(_ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        if not payload:
            return
        for p in sorted(payload.get("created", []), key=len, reverse=True):
            path = Path(p)
            try:
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except (FileNotFoundError, OSError) as e:
                log.info("make_dirs undo: skipping %s (%s)", p, e)
    return Step("make_dirs", do, undo)


def _step_copy_payload() -> Step:
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        # Python source trees consumed at runtime
        _copy_tree(ctx.dev_root / "orchestrator", ctx.install_root / "orchestrator")
        _copy_tree(ctx.dev_root / "analyzer",     ctx.install_root / "analyzer")
        _copy_tree(ctx.dev_root / "interceptors" / "browser",
                   ctx.install_root / "interceptors" / "browser")
        # Python embeddable distribution
        _copy_tree(ctx.dev_root / "python-embed", ctx.install_root / "python")

        # .NET publishes — copy each publish dir whole so the .deps.json /
        # .runtimeconfig.json / native side-DLLs all land beside the .exe.
        controller_src = ctx.artifacts["controller_exe"].parent
        clipboard_src  = ctx.artifacts["clipboard_exe"].parent
        transfer_src   = ctx.artifacts["transfer_agent_exe"].parent
        _copy_tree(controller_src, ctx.install_root / "bin" / "Controller")
        _copy_tree(clipboard_src,  ctx.install_root / "bin" / "Clipboard")
        _copy_tree(transfer_src,   ctx.install_root / "bin" / "TransferAgent")

        # ShellExt is a single DLL; Payload.dll normally ships inside the
        # Controller publish dir (Controller.csproj has CopyPayloadDll target),
        # but copy it explicitly as a safety net if the publish skipped it.
        shutil.copy2(ctx.artifacts["shell_extension_dll"],
                     ctx.install_root / "bin" / "ShellExt" / "DlpShellExt.dll")
        payload_dst = ctx.install_root / "bin" / "Controller" / "Payload.dll"
        if not payload_dst.exists():
            shutil.copy2(ctx.artifacts["payload_dll"], payload_dst)

        _write_installed_config(ctx)
        return {"install_root": str(ctx.install_root)}

    def undo(ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        target = Path(payload["install_root"]) if payload else ctx.install_root
        if target.exists():
            _rmtree_with_retry(target)
    return Step("copy_payload", do, undo)


# Path keys rewritten to install-layout-relative strings. Shared by the
# installed config (_write_installed_config) and the deployable bundle config
# (build_bundle_config). `mitmdump_exe` is NOT here because its embed directory
# name differs: "python/..." once installed vs "python-embed/..." in the bundle.
_INSTALL_LAYOUT_PATHS = {
    "addon_script":        "interceptors/browser/addon.py",
    "clipboard_exe":       "bin/Clipboard/ClipboardInterceptor.exe",
    "controller_exe":      "bin/Controller/UsbDlpController.exe",
    "transfer_agent_exe":  "bin/TransferAgent/DlpTransferAgent.exe",
    "shell_extension_dll": "bin/ShellExt/DlpShellExt.dll",
    "payload_dll":         "bin/Controller/Payload.dll",
}


def _write_installed_config(ctx: InstallContext) -> None:
    """Write ``install_root/config.yaml`` with ``paths:`` rewritten to install-mode.

    Same shape as dev config, just install-root-relative paths. Comments are
    not preserved (yaml.safe_dump drops them) — acceptable for an operational
    file that's read but not human-edited."""
    new_raw = dict(ctx.config.raw)
    paths = dict(new_raw.get("paths", {}))
    paths.update(_INSTALL_LAYOUT_PATHS)
    paths["mitmdump_exe"] = "python/Scripts/mitmdump.exe"   # embed installed at python/
    new_raw["paths"] = paths
    install_section = dict(new_raw.get("install") or {})
    install_section["install_root"] = str(ctx.install_root)
    new_raw["install"] = install_section
    out = ctx.install_root / "config.yaml"
    out.write_text(yaml.safe_dump(new_raw, sort_keys=False), encoding="utf-8")
    log.info("wrote installed config to %s", out)


def build_bundle_config(src_config_path: str | Path, dest_config_path: str | Path) -> None:
    """Write a VM-ready ``config.yaml`` for a deployable bundle (see package-bundle.ps1).

    The bundle is laid out as the install tree (``bin/...``, ``python-embed/``,
    ``orchestrator/`` …), so ``--install --config <bundle>/config.yaml`` resolves
    every artifact and proceeds with no edits. This rewrites:
      - ``paths.*`` to bundle-relative (shared ``_INSTALL_LAYOUT_PATHS``; mitmdump
        points at ``python-embed/Scripts/mitmdump.exe``, the bundle's embed dir),
      - ``paths.log_dir`` → "" (so it defaults to %PROGRAMDATA%\\DLP\\logs on the VM),
      - ``policies_file`` → ``analyzer/policies.yaml``,
      - ``install.install_root`` → "" (so it defaults to %ProgramFiles%\\DLP).
    (browser.temp_dir is no longer a config key — it is hardcoded to the system
    %TEMP% in interceptors/browser/config.py — so nothing to neutralize here.)
    Everything else is copied verbatim, so new config sections flow through.
    """
    src = Path(src_config_path)
    with src.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    paths = dict(raw.get("paths", {}))
    paths.update(_INSTALL_LAYOUT_PATHS)
    paths["mitmdump_exe"] = "python-embed/Scripts/mitmdump.exe"
    paths["log_dir"] = ""
    raw["paths"] = paths

    raw["policies_file"] = "analyzer/policies.yaml"

    install_section = dict(raw.get("install") or {})
    install_section["install_root"] = ""
    raw["install"] = install_section

    dest = Path(dest_config_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    log.info("wrote bundle config to %s", dest)


def _step_bootstrap_ca() -> Step:
    # Invoke the mitmproxy CertStore API directly instead of going through
    # mitmdump's CLI. Empirically (mitmproxy 12.2.3), neither --no-server nor
    # --listen-port 0 triggers the addon-stack init that calls
    # CertStore.from_store, so mitmdump exits cleanly without generating any
    # CA files. The CertStore class is a public, documented API
    # (https://docs.mitmproxy.org/stable/api/mitmproxy/certs.html); calling
    # from_store(path, basename, key_size, passphrase) synchronously creates
    # mitmproxy-ca.pem + mitmproxy-ca-cert.cer + mitmproxy-ca-cert.p12 +
    # mitmproxy-ca.p12 + mitmproxy-dhparam.pem in `path`. No port binding,
    # no event loop, sub-second total.
    _SCRIPT = (
        "import sys\n"
        "from pathlib import Path\n"
        "from mitmproxy.certs import CertStore\n"
        "p = Path(sys.argv[1])\n"
        "p.mkdir(parents=True, exist_ok=True)\n"
        "CertStore.from_store(p, 'mitmproxy', 2048, None)\n"
    )

    def do(ctx: InstallContext) -> dict[str, Any] | None:
        cer = ctx.mitm_confdir / "mitmproxy-ca-cert.cer"
        if cer.is_file():
            log.info("bootstrap_ca: %s already present; skipping generation", cer)
            return {"cer": str(cer), "confdir": str(ctx.mitm_confdir)}
        python_exe = ctx.install_root / "python" / "python.exe"
        if not python_exe.is_file():
            # Recovery scenarios: fall back to the dev-tree embed.
            python_exe = ctx.dev_root / "python-embed" / "python.exe"
        log.info("bootstrap_ca: invoking CertStore.from_store(%s, 'mitmproxy', 2048)",
                 ctx.mitm_confdir)
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
                "mitmproxy may have changed its CA file naming. Check "
                f"{ctx.mitm_confdir} for the actual filenames.")
        log.info("bootstrap_ca: cert generated at %s", cer)
        return {"cer": str(cer), "confdir": str(ctx.mitm_confdir)}

    def undo(ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        # Remove the entire mitmproxy confdir we generated.
        confdir = Path((payload or {}).get("confdir") or ctx.mitm_confdir)
        if confdir.exists():
            try:
                shutil.rmtree(confdir)
            except (FileNotFoundError, OSError) as e:
                log.info("bootstrap_ca undo: rmtree %s skipped (%s)", confdir, e)
    return Step("bootstrap_ca", do, undo)


def _step_install_root_ca() -> Step:
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        cer = ctx.mitm_confdir / "mitmproxy-ca-cert.cer"
        if not cer.is_file():
            raise FileNotFoundError(f"install_root_ca: CA cert missing: {cer}")
        thumb = _cert_thumbprint(cer)
        inst = ctx.config.raw.get("install") or {}
        thumb_file = ctx.state_dir / inst.get("ca_thumbprint_file", "installed_ca.txt")
        thumb_file.parent.mkdir(parents=True, exist_ok=True)
        thumb_file.write_text(thumb, encoding="utf-8")
        log.info("install_root_ca: SHA-1 thumbprint=%s -> %s", thumb, thumb_file)

        result = subprocess.run(
            ["certutil", "-addstore", "-f", "Root", str(cer)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(
                f"certutil -addstore failed (exit={result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}")
        log.info("install_root_ca: cert added to LocalMachine\\Root")
        return {"thumbprint": thumb, "thumb_file": str(thumb_file)}

    def undo(ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        thumb = (payload or {}).get("thumbprint")
        thumb_file_str = (payload or {}).get("thumb_file") or str(
            ctx.state_dir / "installed_ca.txt")
        thumb_file = Path(thumb_file_str)
        if not thumb:
            try:
                thumb = thumb_file.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                log.warning(
                    "install_root_ca undo: no recorded thumbprint; clean up manually "
                    "via certmgr.msc -> Trusted Root -> search 'mitmproxy'")
                return
        result = subprocess.run(
            ["certutil", "-delstore", "Root", thumb],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        combined = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and "0x80092004" not in combined:
            log.warning(
                "certutil -delstore returned %s; stdout=%r stderr=%r",
                result.returncode, result.stdout, result.stderr)
        try:
            thumb_file.unlink()
        except FileNotFoundError:
            pass
    return Step("install_root_ca", do, undo)


def _step_backup_proxy() -> Step:
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        backup: dict[str, Any] = {
            "ProxyEnable": None, "ProxyServer": None, "ProxyOverride": None,
        }
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PROXY_KEY,
                                0, winreg.KEY_READ) as k:
                for name in backup:
                    try:
                        val, _ = winreg.QueryValueEx(k, name)
                        backup[name] = val
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            log.warning("backup_proxy: HKCU\\%s missing; backup empty", _PROXY_KEY)
        inst = ctx.config.raw.get("install") or {}
        backup_file = ctx.state_dir / inst.get("proxy_backup_file", "proxy_backup.json")
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        backup_file.write_text(json.dumps(backup, indent=2), encoding="utf-8")
        log.info("backup_proxy: snapshot saved to %s", backup_file)
        return {"backup_file": str(backup_file)}

    def undo(_ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        # The actual restore happens in set_proxy's undo, which reads this
        # file. Here we just delete the backup itself (after set_proxy has
        # already used it — undos run in reverse order).
        path_str = (payload or {}).get("backup_file")
        if path_str:
            try:
                Path(path_str).unlink()
            except FileNotFoundError:
                pass
    return Step("backup_proxy", do, undo)


def _step_set_proxy() -> Step:
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        port = ctx.config.proxy_listen_port
        proxy_server = f"127.0.0.1:{port}"
        bypass = ctx.config.proxy_bypass
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PROXY_KEY,
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, proxy_server)
            winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, bypass)
        log.info("set_proxy: HKCU ProxyEnable=1 ProxyServer=%s ProxyOverride=%s",
                 proxy_server, bypass)
        return {"applied": {"ProxyEnable": 1, "ProxyServer": proxy_server,
                            "ProxyOverride": bypass}}

    def undo(ctx: InstallContext, _payload: dict[str, Any] | None) -> None:
        inst = ctx.config.raw.get("install") or {}
        backup_file = ctx.state_dir / inst.get("proxy_backup_file", "proxy_backup.json")
        try:
            backup = json.loads(backup_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            backup = {"ProxyEnable": None, "ProxyServer": None, "ProxyOverride": None}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PROXY_KEY,
                                0, winreg.KEY_SET_VALUE) as k:
                for name in ("ProxyEnable", "ProxyServer", "ProxyOverride"):
                    val = backup.get(name)
                    if val is None:
                        try:
                            winreg.DeleteValue(k, name)
                        except FileNotFoundError:
                            pass
                    else:
                        kind = winreg.REG_DWORD if isinstance(val, int) else winreg.REG_SZ
                        winreg.SetValueEx(k, name, 0, kind, val)
        except FileNotFoundError:
            pass
        subprocess.run(["netsh", "winhttp", "reset", "proxy"],
                       capture_output=True, check=False)
    return Step("set_proxy", do, undo)


def _step_register_shellext() -> Step:
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        shellext_dll = ctx.install_root / "bin" / "ShellExt" / "DlpShellExt.dll"
        transfer_exe = ctx.install_root / "bin" / "TransferAgent" / "DlpTransferAgent.exe"

        # Entries we add. Each entry is either a (sub)key we created (entire
        # key gets recursively deleted on undo) OR a single named value we
        # added to an existing key (only that value gets cleaned up).
        keys_created: list[dict[str, Any]] = []

        def _create_key(path: str, default_value: str | None = None,
                        values: dict[str, str] | None = None) -> None:
            with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
                if default_value is not None:
                    winreg.SetValueEx(k, "", 0, winreg.REG_SZ, default_value)
                if values:
                    for name, val in values.items():
                        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
            keys_created.append({"path": path})

        def _set_value(path: str, value_name: str, value: str) -> None:
            with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
                winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, value)
            keys_created.append({"path": path, "value_name": value_name})

        # CLSID class registration
        _create_key(rf"Software\Classes\CLSID\{_SHELLEXT_CLSID}",
                    default_value=_SHELLEXT_FRIENDLY)
        _create_key(rf"Software\Classes\CLSID\{_SHELLEXT_CLSID}\InProcServer32",
                    default_value=str(shellext_dll),
                    values={"ThreadingModel": "Apartment"})

        # Context menu handler entries (file + directory)
        _create_key(
            rf"Software\Classes\*\shellex\ContextMenuHandlers\{_SHELLEXT_HANDLER_NAME}",
            default_value=_SHELLEXT_CLSID)
        _create_key(
            rf"Software\Classes\Directory\shellex\ContextMenuHandlers\{_SHELLEXT_HANDLER_NAME}",
            default_value=_SHELLEXT_CLSID)

        # Approved-shellext list (Explorer SAFER policy)
        _set_value(
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Shell Extensions\Approved",
            _SHELLEXT_CLSID, _SHELLEXT_FRIENDLY)

        # DlpContextMenu.cpp reads HKLM\SOFTWARE\DLPAgent\TransferAgentPath first.
        _set_value(r"SOFTWARE\DLPAgent", "TransferAgentPath", str(transfer_exe))

        log.info("register_shellext: wrote %d HKLM entries", len(keys_created))
        return {"keys": keys_created}

    def undo(_ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        if not payload:
            return
        # Iterate in reverse so subkeys come before parents
        for entry in reversed(payload.get("keys", [])):
            path = entry["path"]
            value_name = entry.get("value_name")
            try:
                if value_name is not None:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path,
                                        0, winreg.KEY_SET_VALUE) as k:
                        try:
                            winreg.DeleteValue(k, value_name)
                        except FileNotFoundError:
                            pass
                else:
                    _delete_key_recursive(winreg.HKEY_LOCAL_MACHINE, path)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.info("register_shellext undo: %s skipped (%s)", path, e)
    return Step("register_shellext", do, undo)


def _step_notify_shell() -> Step:
    def _notify() -> None:
        ctypes.windll.shell32.SHChangeNotify(
            _SHCNE_ASSOCCHANGED, _SHCNF_IDLIST, None, None)
    def do(_ctx: InstallContext) -> dict[str, Any] | None:
        _notify()
        return None
    def undo(_ctx: InstallContext, _payload: dict[str, Any] | None) -> None:
        _notify()
    return Step("notify_shell", do, undo)


def _broadcast_env_change() -> None:
    """Tell running processes (Explorer, new shells) that the environment changed
    so a freshly-opened terminal sees the updated PATH. Best-effort."""
    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            _HWND_BROADCAST, _WM_SETTINGCHANGE, 0, "Environment",
            _SMTO_ABORTIFHUNG, 5000, ctypes.byref(ctypes.c_ulong()))
    except Exception as exc:  # noqa: BLE001 — broadcast is advisory only
        log.info("env-change broadcast failed (harmless): %s", exc)


def _step_install_ctl_wrapper() -> Step:
    """Write <install_root>\\dlp-ctl.cmd so the operator runs the CLI via the
    bundled embed Python (which has pywin32) instead of a bare `python`."""
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        path = ctx.install_root / _CTL_WRAPPER_NAME
        path.write_text(_CTL_WRAPPER_BODY, encoding="ascii", newline="\r\n")
        log.info("install_ctl_wrapper: wrote %s", path)
        return {"path": str(path)}

    def undo(ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        target = Path((payload or {}).get("path", ctx.install_root / _CTL_WRAPPER_NAME))
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.info("install_ctl_wrapper undo: skipping %s (%s)", target, exc)
    return Step("install_ctl_wrapper", do, undo)


def _step_install_uninstall_wrapper() -> Step:
    """Write <install_root>\\uninstall.cmd so the agent can be removed even if the
    deploy bundle is gone, and — crucially — so uninstall runs from the INSTALLED
    python (allowed by the App Control self-protect policy) rather than the bundle's
    embed python (blocked by WDAC under a deployed enforcement policy). The script
    self-relaunches from %TEMP% so it doesn't delete itself mid-run (see
    ``_UNINSTALL_WRAPPER_BODY``)."""
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        path = ctx.install_root / _UNINSTALL_WRAPPER_NAME
        body = _UNINSTALL_WRAPPER_BODY.replace("{install_root}", str(ctx.install_root))
        path.write_text(body, encoding="ascii", newline="\r\n")
        log.info("install_uninstall_wrapper: wrote %s", path)
        return {"path": str(path)}

    def undo(ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        target = Path((payload or {}).get("path",
                                          ctx.install_root / _UNINSTALL_WRAPPER_NAME))
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.info("install_uninstall_wrapper undo: skipping %s (%s)", target, exc)
    return Step("install_uninstall_wrapper", do, undo)


def _step_add_to_path() -> Step:
    """Append <install_root> to the machine PATH so `dlp-ctl` resolves anywhere.

    Reads/writes HKLM ...\\Session Manager\\Environment Path as REG_EXPAND_SZ.
    winreg.QueryValueEx returns REG_EXPAND_SZ *unexpanded*, so existing %vars%
    (e.g. %SystemRoot%) are preserved on write-back.
    """
    def _norm(p: str) -> str:
        return os.path.normcase(p.strip().rstrip("\\"))

    def do(ctx: InstallContext) -> dict[str, Any] | None:
        entry = str(ctx.install_root)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ENV_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
            try:
                cur, _typ = winreg.QueryValueEx(k, "Path")
            except FileNotFoundError:
                cur = ""
            parts = [p for p in cur.split(";") if p]
            if any(_norm(p) == _norm(entry) for p in parts):
                log.info("add_to_path: %s already present", entry)
                return {"added": False, "entry": entry}
            new_val = cur + (";" if cur and not cur.endswith(";") else "") + entry
            winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, new_val)
        _broadcast_env_change()
        log.info("add_to_path: appended %s to machine PATH", entry)
        return {"added": True, "entry": entry}

    def undo(ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        if not payload or not payload.get("added"):
            return
        entry = payload.get("entry", str(ctx.install_root))
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ENV_KEY, 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as k:
                cur, _typ = winreg.QueryValueEx(k, "Path")
                parts = [p for p in cur.split(";") if p and _norm(p) != _norm(entry)]
                winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(parts))
            _broadcast_env_change()
            log.info("add_to_path undo: removed %s from machine PATH", entry)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.info("add_to_path undo: skipping (%s)", exc)
    return Step("add_to_path", do, undo)


# ─── AC-5: App Control channel install steps ─────────────────────────────────


def _default_dism_runner(mum_path: str) -> "tuple[int, str]":
    """Install one servicing package offline via DISM. Uses the absolute
    ``System32\\Dism.exe`` path (with a bare-name fallback, mirroring
    ``deployer.citool_path()``) so it resolves regardless of PATH. Returns
    ``(returncode, stdout+stderr)`` — the injectable seam tests replace."""
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    cand = os.path.join(windir, "System32", "Dism.exe")
    exe = cand if os.path.isfile(cand) else "dism"
    proc = subprocess.run(
        [exe, "/online", "/norestart", f"/add-package:{mum_path}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _step_appcontrol_dirs() -> Step:
    """Create the App Control drop-folder tree under
    ``%ProgramData%\\DLP\\appcontrol``. The running channel also mkdirs these on
    start (``channel.start``), but this step registers them with the installer so
    uninstall strips the WHOLE tree — inbox/rejected/staging plus the operator's
    allow/deny lists and any pending or rejected pushes (parent decision 7).
    Resolution goes through ``app_control.paths`` so it matches the channel
    byte-for-byte (same single source of truth the AC-4 builder shares)."""
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        from orchestrator.app_control import paths as ac_paths
        root = ac_paths.appcontrol_root(ctx.config)
        targets = [root, ac_paths.inbox_dir(ctx.config),
                   ac_paths.rejected_dir(ctx.config), ac_paths.staging_dir(ctx.config)]
        created: list[str] = []
        for d in targets:
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
                created.append(str(d))
        log.info("appcontrol_dirs: ensured %s (+inbox/rejected/staging)", root)
        return {"root": str(root), "created": created}

    def undo(ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        from orchestrator.app_control import paths as ac_paths
        root_str = (payload or {}).get("root") or str(ac_paths.appcontrol_root(ctx.config))
        root = Path(root_str)
        if root.exists():
            _rmtree_with_retry(root)
            log.info("appcontrol_dirs undo: removed %s", root)
    return Step("appcontrol_dirs", do, undo)


def _configci_available(powershell: str = "powershell") -> bool:
    """True if the ConfigCI module is usable — i.e. ``ConvertFrom-CIPolicy`` resolves.
    This is the real success criterion for enable_configci (not per-package DISM exit
    codes). Probed in a fresh **Windows PowerShell** process (ConfigCI is a Windows
    PowerShell 5.1 module — the same host ``dlp-ctl appcontrol build`` compiles in)."""
    try:
        proc = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command",
             "if (Get-Command ConvertFrom-CIPolicy -ErrorAction SilentlyContinue) "
             "{ exit 0 } else { exit 9 }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return False
    return proc.returncode == 0


def _step_enable_configci(
        dism_runner: Callable[[str], "tuple[int, str]"] | None = None,
        packages_dir: str | Path | None = None,
        probe: Callable[[], bool] | None = None) -> Step:
    """Enable the ConfigCI PowerShell module offline so on-endpoint ``dlp-ctl
    appcontrol build`` can compile policies (``ConvertFrom-CIPolicy`` /
    ``New-CIPolicy``). Win11 Home stages the ConfigCI ``*.mum`` servicing packages
    but does not install them; ``dism /add-package`` installs them with no reboot
    (AC-1 VM-proven).

    The servicing dir holds SEVERAL ConfigCI revisions (e.g. ``26100.1591`` /
    ``.8246`` / ``.8521``); only the one matching the current cumulative-update level
    installs — the others fail "not applicable" (e.g. DISM rc ``14107``). So
    per-package DISM failures are EXPECTED and non-fatal (this mirrors the AC-1
    PowerShell ``gci | %`` loop, which continues past them). The success test is a
    post-loop **probe**: ``ConvertFrom-CIPolicy`` must resolve.

    FAIL-CLOSED (decision D1): when app_control is enabled and ConfigCI is still NOT
    available after the attempt, the step raises -> the transactional driver rolls the
    install back. When ``app_control.enabled`` is false the step is skipped entirely
    (the escape hatch for a box that genuinely can't enable ConfigCI — the rest of the
    agent still installs). Idempotent: if ConfigCI is already available (a reinstall,
    or an image that ships it) the DISM loop is skipped. ``undo`` is a deliberate
    no-op (leaving ConfigCI enabled is benign). ``dism_runner`` / ``packages_dir`` /
    ``probe`` are injectable for tests."""
    runner = dism_runner or _default_dism_runner
    check = probe or _configci_available

    def do(ctx: InstallContext) -> dict[str, Any] | None:
        if not ctx.config.app_control_enabled:
            log.info("enable_configci: app_control disabled in config; skipping "
                     "ConfigCI enable (on-endpoint `dlp-ctl appcontrol build` stays "
                     "unavailable until app_control is enabled).")
            return {"skipped": True}
        if check():
            log.info("enable_configci: ConfigCI already available; nothing to do")
            return {"already_available": True}
        windir = os.environ.get("SystemRoot", r"C:\Windows")
        pkg_dir = (Path(packages_dir) if packages_dir
                   else Path(windir) / "servicing" / "Packages")
        mums = sorted(pkg_dir.glob("*ConfigCI*.mum"))
        log.info("enable_configci: ConfigCI not yet available; attempting %d servicing "
                 "package(s) via DISM (down-level revisions will fail 'not applicable' "
                 "and are skipped — this can take a minute)", len(mums))
        added: list[str] = []
        failed: list[str] = []
        for mum in mums:
            rc, out = runner(str(mum))
            if rc in _DISM_SUCCESS_CODES:
                added.append(mum.name)
            else:
                # Down-level / not-applicable revision — expected; keep going so the
                # applicable revision still installs (AC-1 loop behavior).
                failed.append(mum.name)
                log.info("enable_configci: %s not applicable (rc=%s); continuing",
                         mum.name, rc)
        if not check():
            raise RuntimeError(
                "enable_configci: ConfigCI still unavailable after attempting "
                f"{len(mums)} package(s) under {pkg_dir} (added={added or 'none'}, "
                f"failed={failed or 'none'}); on-endpoint `dlp-ctl appcontrol build` "
                f"cannot work. {_DISM_HINT}  Or set app_control.enabled: false in "
                "config.yaml to skip the channel entirely.")
        log.info("enable_configci: ConfigCI available (added %d package(s), %d not "
                 "applicable); ConvertFrom-CIPolicy ready for on-endpoint build",
                 len(added), len(failed))
        return {"packages_added": added, "packages_failed": failed}

    return Step("enable_configci", do, _noop_undo)


def _step_appcontrol_policy_guard(citool_runner: Callable | None = None) -> Step:
    """Strip any deployed App Control policy at uninstall. Install deploys NO policy
    (parent decision 7 — the channel is idle until the first push), so ``do`` only
    records the base PolicyID in the manifest. But by uninstall time the operator may
    have deployed an enforcement policy via ``dlp-ctl appcontrol apply``; a leftover
    WDAC policy with no agent to manage it would keep blocking apps. ``undo`` runs the
    AC-3 deployer's ``remove()`` (``citool --remove-policy``, no reboot on 24H2+, with
    the AllowAll-neutralizer fallback) and deletes the status record (decision D6).

    Placed immediately before ``install_service`` (D3) so its undo runs AFTER the
    service is stopped and BEFORE ``copy_payload`` removes the install tree — which
    holds ``base.xml`` + the pre-compiled ``neutralizer.cip`` the fallback needs.
    ``citool_runner`` is injectable for tests."""
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        from orchestrator.app_control import policy_xml as px
        policy_id = px.get_policy_id(px.load_base_policy())
        log.info("appcontrol_policy_guard: recorded policy id %s for uninstall removal",
                 policy_id)
        return {"policy_id": policy_id}

    def undo(ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        from orchestrator.app_control import policy_xml as px
        from orchestrator.app_control.deployer import Deployer
        policy_id = (payload or {}).get("policy_id")
        if not policy_id:
            try:
                policy_id = px.get_policy_id(px.load_base_policy())
            except Exception as exc:  # noqa: BLE001
                log.warning("appcontrol_policy_guard undo: cannot resolve policy id "
                            "(%s); skipping policy removal", exc)
                return
        status_path = ctx.state_dir / "appcontrol_status.json"
        try:
            dep = Deployer(status_path=status_path, policy_id=policy_id,
                           runner=citool_runner)
            removed = dep.remove()  # citool --remove-policy + neutralizer fallback
            log.info("appcontrol_policy_guard undo: deployer.remove() -> %s", removed)
        except Exception:  # noqa: BLE001 — uninstall must continue regardless
            log.exception("appcontrol_policy_guard undo: remove() raised; continuing")
        try:
            status_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.info("appcontrol_policy_guard undo: status unlink skipped (%s)", exc)
    return Step("appcontrol_policy_guard", do, undo)


def _step_install_service() -> Step:
    def do(ctx: InstallContext) -> dict[str, Any] | None:
        python_exe = ctx.install_root / "python" / "python.exe"
        config_yaml = ctx.install_root / "config.yaml"
        bin_path = (
            f'"{python_exe}" -m orchestrator --service '
            f'--config "{config_yaml}"'
        )
        create = subprocess.run(
            ["sc.exe", "create", ctx.service_name,
             "binPath=", bin_path,
             "start=", ctx.service_start_type,
             "DisplayName=", ctx.service_display],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if create.returncode == 0:
            log.info("install_service: created %s", ctx.service_name)
        elif create.returncode == _ERROR_SERVICE_EXISTS:
            log.info("install_service: %s exists; updating via sc config",
                     ctx.service_name)
            update = subprocess.run(
                ["sc.exe", "config", ctx.service_name,
                 "binPath=", bin_path,
                 "start=", ctx.service_start_type,
                 "DisplayName=", ctx.service_display],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            if update.returncode != 0:
                raise RuntimeError(
                    f"sc.exe config {ctx.service_name} failed "
                    f"(exit={update.returncode}): stdout={update.stdout!r} "
                    f"stderr={update.stderr!r}")
        else:
            raise RuntimeError(
                f"sc.exe create {ctx.service_name} failed "
                f"(exit={create.returncode}): stdout={create.stdout!r} "
                f"stderr={create.stderr!r}")
        subprocess.run(
            ["sc.exe", "description", ctx.service_name, ctx.service_desc],
            capture_output=True, check=False)
        # Start the service now (best-effort) so it's Running right after install
        # without waiting for the next boot. A start failure must NOT roll back an
        # otherwise-good install — the service is start=auto and will come up on
        # reboot regardless; just log a warning.
        start = subprocess.run(
            ["sc.exe", "start", ctx.service_name],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if start.returncode in (0, _ERROR_SERVICE_ALREADY_RUNNING):
            log.info("install_service: started %s", ctx.service_name)
        else:
            log.warning(
                "install_service: sc.exe start %s returned %d; start it manually "
                "with `Start-Service %s` (it will also auto-start on reboot): %s",
                ctx.service_name, start.returncode, ctx.service_name,
                (start.stderr or start.stdout).strip())
        return {"service_name": ctx.service_name}

    def undo(ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        name = (payload or {}).get("service_name", ctx.service_name)
        stop = subprocess.run(
            ["sc.exe", "stop", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if stop.returncode not in (0, _ERROR_SERVICE_NOT_ACTIVE,
                                   _ERROR_SERVICE_DOES_NOT_EXIST):
            log.info("sc stop %s returned %s (continuing to delete)",
                     name, stop.returncode)
        delete = subprocess.run(
            ["sc.exe", "delete", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if delete.returncode not in (0, _ERROR_SERVICE_DOES_NOT_EXIST):
            log.warning("sc delete %s returned %s; stderr=%r",
                        name, delete.returncode, delete.stderr)
    return Step("install_service", do, undo)


# ─── Module-level handle exposed for harness testing ────────────────────────
# scripts/harness/test_installer.py imports _drive_install/_drive_uninstall
# directly to exercise the rollback logic without needing real registry,
# certutil, or sc.exe access.


__all__ = [
    "InstallContext", "Step",
    "run_install", "run_uninstall",
    "_drive_install", "_drive_uninstall",
    "_build_context", "_build_default_steps",
]
