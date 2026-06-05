"""WTS session enumeration + CreateProcessAsUser helpers (Phase E session bridge).

The LocalSystem ``DLPAgent`` service lives in Session 0, which has no interactive
window-station / desktop / clipboard. Children that touch the interactive desktop
(``ClipboardInterceptor`` always; ``Controller`` if the E0 spike says cross-session
injection is unavailable) must therefore be launched *into* each logged-on user's
session with ``CreateProcessAsUser`` using a token obtained from that session.

This module is the thin Win32 layer:

- :func:`enumerate_interactive_sessions` — active sessions to spawn into.
- :func:`user_token_for_session` / :func:`linked_token` — primary tokens for
  ``CreateProcessAsUser`` (the linked/elevated variant carries
  ``SeCreateGlobalPrivilege``, needed by a user-session Controller — fallback B).
- :func:`sid_for_token` — the SID string used to address ``HKEY_USERS\\<SID>``.
- :func:`spawn_as_user` — the actual ``CreateProcessAsUser`` call, returning a
  :class:`SessionProcess` that exposes the small ``poll``/``wait``/``terminate``
  subset the Supervisor relies on.
- :func:`set_session_proxy` / :func:`restore_session_proxy` — per-session HKCU
  proxy redirect, addressed via ``HKEY_USERS\\<SID>`` (the hive is already mounted
  for a logged-on user, so no ``RegLoadKey`` is needed).

All Win32 plumbing here is exercised by manual smoke (Phase E E7); the Supervisor's
session table logic is unit-tested with fakes injected in place of these functions.
"""
from __future__ import annotations

import json
import logging
import subprocess
import winreg
from pathlib import Path

import win32con
import win32event
import win32process
import win32profile
import win32security
import win32ts

log = logging.getLogger(__name__)

_PROXY_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


class SessionProcess:
    """Popen-subset adapter around a ``CreateProcessAsUser`` result.

    The Supervisor only needs ``pid``, ``poll()``, ``wait()``, ``terminate()`` and
    ``returncode`` — this exposes exactly those over a raw process handle so the
    same watcher/restart code can drive both Popen (Session-0) and session children.
    """

    def __init__(self, h_process, h_thread, pid: int) -> None:
        self._h_process = h_process
        self._h_thread = h_thread
        self.pid = pid
        self._returncode: int | None = None

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        if win32event.WaitForSingleObject(self._h_process, 0) == win32event.WAIT_TIMEOUT:
            return None
        self._returncode = win32process.GetExitCodeProcess(self._h_process)
        return self._returncode

    def wait(self, timeout: float | None = None) -> int | None:
        ms = win32event.INFINITE if timeout is None else int(timeout * 1000)
        win32event.WaitForSingleObject(self._h_process, ms)
        return self.poll()

    def terminate(self) -> None:
        try:
            win32process.TerminateProcess(self._h_process, 1)
        except win32process.error as exc:  # already gone, etc.
            log.debug("SessionProcess.terminate pid=%s: %s", self.pid, exc)

    @property
    def returncode(self) -> int | None:
        return self._returncode


def enumerate_interactive_sessions() -> list[int]:
    """Return the ids of active, interactive (non-Session-0) sessions."""
    sessions = win32ts.WTSEnumerateSessions(win32ts.WTS_CURRENT_SERVER_HANDLE)
    return [
        s["SessionId"]
        for s in sessions
        if s["State"] == win32ts.WTSActive and s["SessionId"] != 0
    ]


def user_token_for_session(session_id: int):
    """Primary token for the user logged on to ``session_id``.

    ``WTSQueryUserToken`` requires ``SE_TCB_PRIVILEGE`` — LocalSystem has it. We
    duplicate the result to a primary token with MAXIMUM_ALLOWED so it is valid
    for ``CreateProcessAsUser``.
    """
    impersonation = win32ts.WTSQueryUserToken(session_id)
    try:
        return win32security.DuplicateTokenEx(
            impersonation,
            win32security.SecurityImpersonation,
            win32con.MAXIMUM_ALLOWED,
            win32security.TokenPrimary,
            None,
        )
    finally:
        impersonation.Close()


def linked_token(token):
    """The elevated/linked primary token for a UAC-split admin (fallback B).

    Only the linked (high-integrity) token carries ``SeCreateGlobalPrivilege``,
    which a user-session Controller needs to create the ``Global\\UsbDlp*`` objects.
    Raises if the account has no linked token (e.g. a plain standard user) — the
    caller (Supervisor) treats that as "cannot host Controller in this session".
    """
    linked = win32security.GetTokenInformation(token, win32security.TokenLinkedToken)
    return win32security.DuplicateTokenEx(
        linked,
        win32security.SecurityImpersonation,
        win32con.MAXIMUM_ALLOWED,
        win32security.TokenPrimary,
        None,
    )


def sid_for_token(token) -> str:
    """SID string for the user owning ``token`` (for ``HKEY_USERS\\<SID>``)."""
    sid, _attrs = win32security.GetTokenInformation(token, win32security.TokenUser)
    return win32security.ConvertSidToStringSid(sid)


def spawn_as_user(
    token,
    exe: str,
    args: list[str],
    cwd: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> SessionProcess:
    """``CreateProcessAsUser`` into the session that ``token`` belongs to.

    ``lpDesktop = winsta0\\default`` puts the child on the user's interactive
    desktop so a clipboard listener / window hooks work. The environment block is
    built from the user's profile via ``CreateEnvironmentBlock`` (needs the
    ``CREATE_UNICODE_ENVIRONMENT`` flag since pywin32 hands back a dict).
    """
    env = win32profile.CreateEnvironmentBlock(token, False)
    if env_extra:
        env = {**env, **env_extra}

    si = win32process.STARTUPINFO()
    si.lpDesktop = r"winsta0\default"

    flags = win32con.CREATE_UNICODE_ENVIRONMENT | win32con.CREATE_NO_WINDOW
    cmdline = subprocess.list2cmdline([exe, *args])

    h_process, h_thread, pid, _tid = win32process.CreateProcessAsUser(
        token,        # hToken
        exe,          # appName
        cmdline,      # commandLine
        None,         # processAttributes
        None,         # threadAttributes
        False,        # bInheritHandles
        flags,        # dwCreationFlags
        env,          # newEnvironment
        cwd,          # currentDirectory
        si,           # startupinfo
    )
    log.info("spawn_as_user: %s pid=%d (session token)", exe, pid)
    return SessionProcess(h_process, h_thread, pid)


# ── Per-session proxy ────────────────────────────────────────────────────────


def _proxy_backup_path(state_dir: Path, sid: str) -> Path:
    return state_dir / f"proxy_backup_{sid}.json"


def set_session_proxy(sid: str, proxy_server: str, bypass: str, state_dir: Path) -> None:
    """Back up then redirect a logged-on user's HKCU proxy via ``HKEY_USERS\\<SID>``.

    Mirrors the install-time logic in ``installer.py:_step_set_proxy`` but keyed by
    SID instead of the implicit installing user, so the running service is the
    runtime authority for every active session (R6).
    """
    key_path = rf"{sid}\{_PROXY_SUBKEY}"

    backup: dict[str, object | None] = {
        "ProxyEnable": None, "ProxyServer": None, "ProxyOverride": None,
    }
    try:
        with winreg.OpenKey(winreg.HKEY_USERS, key_path, 0, winreg.KEY_READ) as k:
            for name in backup:
                try:
                    backup[name], _ = winreg.QueryValueEx(k, name)
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        log.warning("set_session_proxy: HKU\\%s missing; backup empty", key_path)

    state_dir.mkdir(parents=True, exist_ok=True)
    _proxy_backup_path(state_dir, sid).write_text(json.dumps(backup, indent=2),
                                                  encoding="utf-8")

    with winreg.OpenKey(winreg.HKEY_USERS, key_path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, proxy_server)
        winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, bypass)
    log.info("set_session_proxy: SID=%s ProxyServer=%s", sid, proxy_server)


def restore_session_proxy(sid: str, state_dir: Path) -> None:
    """Restore a session's proxy values from its per-SID backup (idempotent)."""
    backup_path = _proxy_backup_path(state_dir, sid)
    try:
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        backup = {"ProxyEnable": None, "ProxyServer": None, "ProxyOverride": None}

    key_path = rf"{sid}\{_PROXY_SUBKEY}"
    try:
        with winreg.OpenKey(winreg.HKEY_USERS, key_path, 0, winreg.KEY_SET_VALUE) as k:
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
        log.info("restore_session_proxy: HKU\\%s gone; nothing to restore", key_path)
    try:
        backup_path.unlink()
    except FileNotFoundError:
        pass
    log.info("restore_session_proxy: SID=%s restored", sid)
