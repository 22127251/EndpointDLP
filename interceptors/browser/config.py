"""Loader for the browser addon's slice of the central config.yaml.

Replaces the legacy interceptors/browser/config.yaml. The browser addon now
reads:
  - the top-level `data_pipe` (the addon connects to this for analyses)
  - the `browser:` section (all its own settings)
from the central file located via DLP_CONFIG_PATH or walk-up.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

_ENV_VAR = "DLP_CONFIG_PATH"
_FILE_NAME = "config.yaml"
_MAX_WALK_UP_LEVELS = 8

# ---------------------------------------------------------------------------
# Hardcoded browser-channel settings (intentionally NOT admin-exposed in
# config.yaml). These are rarely changed; baking them in keeps the central
# config clean. The only browser settings still read from config.yaml are
# pipe_timeout_ms and failure_mode (see load_config / config_from_ctl_payload).
# ---------------------------------------------------------------------------
_TEMP_DIR = ""                       # "" → system %TEMP% (see resolved_temp_dir)
_MIN_UPLOAD_SIZE_BYTES = 1024

# Domains to skip unconditionally (analytics / telemetry / logging beacons).
_DOMAIN_BLOCKLIST = [
    "google-analytics.com",
    "analytics.google.com",
    "beacons.gcp.gvt2.com",
    "beacons.gvt2.com",
    "beacons3.gvt2.com",
    "ssl.gstatic.com",
]

# URL path/query keywords that signal a real upload endpoint.
_UPLOAD_URL_KEYWORDS = ["upload", "attach", "import"]

# File-type allow-list: a file is inspected if its extension OR MIME matches.
_EXTENSIONS = [
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".zip", ".rar", ".7z", ".tar", ".gz",
    ".csv", ".txt", ".md",
]

_MIME_TYPES = [
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "application/zip",
    "application/x-rar-compressed",
    "application/x-7z-compressed",
    "application/x-tar",
    "application/gzip",
    "text/plain",
    "text/csv",
    "text/markdown",
    "text/x-markdown",
]


@dataclass
class Config:
    """Browser addon config.

    Only ``pipe_name`` / ``timeout_seconds`` / ``failure_mode`` come from
    config.yaml. The upload-filter fields below are HARDCODED (the module
    constants above) and are no longer read from the yaml or the ctl payload —
    they default to the constants and load_config/config_from_ctl_payload leave
    them at their defaults.
    """
    pipe_name: str = ""
    timeout_seconds: float = 5.0
    failure_mode: str = "fail_closed"
    temp_dir: str = _TEMP_DIR
    min_upload_size_bytes: int = _MIN_UPLOAD_SIZE_BYTES
    extensions: List[str] = field(default_factory=lambda: list(_EXTENSIONS))
    mime_types: List[str] = field(default_factory=lambda: list(_MIME_TYPES))
    domain_blocklist: List[str] = field(default_factory=lambda: list(_DOMAIN_BLOCKLIST))
    upload_url_keywords: List[str] = field(default_factory=lambda: list(_UPLOAD_URL_KEYWORDS))

    def resolved_temp_dir(self) -> str:
        return self.temp_dir if self.temp_dir else tempfile.gettempdir()

    def fail_open(self) -> bool:
        return self.failure_mode.lower() == "fail_open"

    def has_type_filter(self) -> bool:
        return bool(self.extensions or self.mime_types)


class ConfigNotFoundError(FileNotFoundError):
    """Raised when no config.yaml passes the sentinel check."""


def find_config_yaml(anchor: str | None = None) -> str:
    """Locate config.yaml using DLP_CONFIG_PATH → walk-up with sentinel check.

    Mirrors DlpShared.ConfigLocator.FindConfigYaml in C#. The sentinel is a
    top-level non-empty `data_pipe` key — without it we'd risk picking up a
    `config.yaml` placed by some unrelated tool in the walk-up path.
    """
    tried: list[tuple[str, str]] = []

    env_path = os.environ.get(_ENV_VAR)
    if env_path:
        if not os.path.exists(env_path):
            tried.append((env_path, "file does not exist"))
        elif _has_data_pipe_sentinel(env_path):
            return env_path
        else:
            tried.append((env_path, "missing data_pipe sentinel"))

    anchor_path = Path(anchor) if anchor else Path(os.path.dirname(os.path.abspath(__file__)))
    current = anchor_path
    for _ in range(_MAX_WALK_UP_LEVELS):
        candidate = current / _FILE_NAME
        if candidate.exists():
            if _has_data_pipe_sentinel(str(candidate)):
                return str(candidate)
            tried.append((str(candidate), "missing data_pipe sentinel"))
        else:
            tried.append((str(candidate), "not found"))
        parent = current.parent
        if parent == current:
            break
        current = parent

    lines = [
        f"Could not locate {_FILE_NAME}.",
        f"Set {_ENV_VAR} or place {_FILE_NAME} in the repo root (must contain a top-level 'data_pipe' key).",
        "Paths tried:",
    ]
    lines.extend(f"  - {p}: {r}" for p, r in tried)
    raise ConfigNotFoundError("\n".join(lines))


def load_config(config_yaml_path: str) -> Config:
    """Read the central config.yaml; project the top-level `data_pipe` + the two
    admin-tunable `browser:` settings (pipe_timeout_ms, failure_mode) into a
    Config. The upload-filter fields are hardcoded (module constants) and are NOT
    read from the file."""
    with open(config_yaml_path, "r", encoding="utf-8") as f:
        root = yaml.safe_load(f) or {}

    pipe_name = root.get("data_pipe", "")
    browser = root.get("browser") or {}

    return Config(
        pipe_name=pipe_name,
        timeout_seconds=float(browser.get("pipe_timeout_ms", 12000)) / 1000.0,
        failure_mode=browser.get("failure_mode", "fail_closed"),
    )


def config_from_ctl_payload(payload: dict, current_pipe_name: str) -> Config:
    """Build a Config from the JSON dict pushed over the ctl-pipe.

    Only pipe_name / timeout / failure_mode are honored; the upload-filter fields
    are hardcoded (module constants). The orchestrator's ctl-pipe broadcast
    already overrides data_pipe back to the in-use value, but we double-check and
    prefer the current pipe name if they differ (defense in depth for decision #7).
    """
    pushed_pipe = payload.get("data_pipe", "") or ""
    if pushed_pipe and pushed_pipe != current_pipe_name:
        # Should never happen — orchestrator overrides. Keep current.
        pushed_pipe = current_pipe_name

    browser = payload.get("browser") or {}

    return Config(
        pipe_name=pushed_pipe or current_pipe_name,
        timeout_seconds=float(browser.get("pipe_timeout_ms", 12000)) / 1000.0,
        failure_mode=browser.get("failure_mode", "fail_closed"),
    )


def _has_data_pipe_sentinel(yaml_path: str) -> bool:
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    v = data.get("data_pipe")
    return isinstance(v, str) and bool(v)
