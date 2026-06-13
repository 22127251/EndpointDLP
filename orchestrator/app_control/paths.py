"""Shared App Control path resolution (Phase AC-4).

Both the running channel (``channel.py``) and the operator CLI builder
(``builder.py``) need to agree on *exactly* where the inbox / staging / status
record / allow+deny lists live — otherwise ``dlp-ctl appcontrol apply`` would drop
a push into a folder the watcher isn't watching. These are pure functions over an
``OrchestratorConfig`` so there is one source of truth.

Resolution mirrors ``installer.py`` / ``supervisor.py``: dirs default under
``%PROGRAMDATA%\\DLP\\appcontrol\\`` (each of inbox/rejected/staging individually
overridable via the ``app_control:`` config section), the deployed-state record
under the install ``state_dir`` (``%PROGRAMDATA%\\DLP\\state`` by default), and the
agent's install root under ``%ProgramFiles%\\DLP``.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import selfprotect


def program_data() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))


def program_files() -> str:
    return os.environ.get("ProgramFiles", r"C:\Program Files")


def appcontrol_root(config) -> Path:
    """``%PROGRAMDATA%\\DLP\\appcontrol`` — the channel's state root. The individual
    inbox/rejected/staging dirs may be overridden in config; this root (and the
    allow/deny lists under it) is fixed."""
    return program_data() / "DLP" / "appcontrol"


def inbox_dir(config) -> Path:
    return Path(config.app_control_inbox_dir or (appcontrol_root(config) / "inbox"))


def rejected_dir(config) -> Path:
    return Path(config.app_control_rejected_dir or (appcontrol_root(config) / "rejected"))


def staging_dir(config) -> Path:
    return Path(config.app_control_staging_dir or (appcontrol_root(config) / "staging"))


def _install_section(config) -> dict:
    return config.raw.get("install") or {}


def state_dir(config) -> Path:
    return Path(_install_section(config).get("state_dir")
               or (program_data() / "DLP" / "state"))


def status_path(config) -> Path:
    """``…\\state\\appcontrol_status.json`` — the deployer's persisted record."""
    return state_dir(config) / "appcontrol_status.json"


def install_root(config) -> str:
    """The agent install tree (self-protect FilePath root)."""
    return _install_section(config).get("install_root") or f"{program_files()}\\DLP"


def dotnet_root(config) -> str:
    """The .NET shared-runtime root (the second self-protect FilePath root)."""
    return selfprotect.default_dotnet_root()


def extra_paths(config) -> list | None:
    """Optional additional self-protect FilePath roots from config (None if empty)."""
    return list(config.app_control_extra_paths or []) or None


def allow_list_path(config) -> Path:
    return appcontrol_root(config) / "allow-list.txt"


def deny_list_path(config) -> Path:
    return appcontrol_root(config) / "deny-list.txt"
