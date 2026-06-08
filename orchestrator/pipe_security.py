"""Shared named-pipe SECURITY_ATTRIBUTES builder.

Factored out of ``server.py`` (Phase C post-impl fix #1) so the data-pipe,
ctl-pipe, and the Phase F admin-pipe can each pick the right DACL from one
place.

Two flavours, selected by ``allow_authenticated_users``:

* ``True``  — SYSTEM + Administrators get FILE_ALL_ACCESS, Authenticated Users
  get FILE_GENERIC_READ | FILE_GENERIC_WRITE. Used by the **data-pipe** (the
  medium-integrity TransferAgent must read+write it) and the **ctl-pipe** (the
  per-session ClipboardInterceptor runs under a plain, non-admin user token and
  must read+write it to subscribe for config hot-reload).
* ``False`` — only SYSTEM + Administrators get FILE_ALL_ACCESS; everyone else is
  denied by omission. Used by the Phase F **admin-pipe** so a non-admin caller
  of ``dlp-ctl`` gets ACCESS_DENIED.
"""
from __future__ import annotations

import ntsecuritycon
import win32security


def build_pipe_sa(allow_authenticated_users: bool) -> win32security.SECURITY_ATTRIBUTES:
    """Build a named-pipe SECURITY_ATTRIBUTES (see module docstring)."""
    dacl = win32security.ACL()
    sys_sid = win32security.CreateWellKnownSid(
        win32security.WinLocalSystemSid, None)
    admins_sid = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid, None)
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sys_sid)
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, admins_sid)
    if allow_authenticated_users:
        auth_users_sid = win32security.CreateWellKnownSid(
            win32security.WinAuthenticatedUserSid, None)
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
