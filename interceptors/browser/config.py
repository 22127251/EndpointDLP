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

_DEFAULT_UPLOAD_KEYWORDS = ["upload", "attach", "import"]
_ENV_VAR = "DLP_CONFIG_PATH"
_FILE_NAME = "config.yaml"
_MAX_WALK_UP_LEVELS = 8


@dataclass
class Config:
    pipe_name: str = ""
    timeout_seconds: float = 5.0
    fail_behavior: str = "block"
    temp_dir: str = ""
    min_upload_size_bytes: int = 1024
    extensions: List[str] = field(default_factory=list)
    mime_types: List[str] = field(default_factory=list)
    domain_blocklist: List[str] = field(default_factory=list)
    upload_url_keywords: List[str] = field(default_factory=lambda: list(_DEFAULT_UPLOAD_KEYWORDS))

    def resolved_temp_dir(self) -> str:
        return self.temp_dir if self.temp_dir else tempfile.gettempdir()

    def fail_open(self) -> bool:
        return self.fail_behavior.lower() == "allow"

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
    """Read the central config.yaml; project the `browser` section + top-level
    `data_pipe` into a Config. Raises on missing required keys."""
    with open(config_yaml_path, "r", encoding="utf-8") as f:
        root = yaml.safe_load(f) or {}

    pipe_name = root.get("data_pipe", "")
    browser = root.get("browser") or {}

    raw_ext = browser.get("extensions") or []
    extensions = [
        e.lower() if e.startswith(".") else f".{e.lower()}"
        for e in raw_ext
    ]
    mime_types = [m.lower() for m in (browser.get("mime_types") or [])]
    domain_blocklist = [d.lower() for d in (browser.get("domain_blocklist") or [])]
    upload_url_keywords = [
        k.lower() for k in (browser.get("upload_url_keywords") or _DEFAULT_UPLOAD_KEYWORDS)
    ]

    return Config(
        pipe_name=pipe_name,
        timeout_seconds=float(browser.get("pipe_timeout_seconds", 5.0)),
        fail_behavior=browser.get("fail_behavior", "block"),
        temp_dir=browser.get("temp_dir", ""),
        min_upload_size_bytes=int(browser.get("min_upload_size_bytes", 1024)),
        extensions=extensions,
        mime_types=mime_types,
        domain_blocklist=domain_blocklist,
        upload_url_keywords=upload_url_keywords,
    )


def config_from_ctl_payload(payload: dict, current_pipe_name: str) -> Config:
    """Build a Config from the JSON dict pushed over the ctl-pipe.

    The orchestrator's ctl-pipe broadcast already overrides data_pipe back to
    the in-use value, but we double-check and prefer the current pipe name if
    they differ (defense in depth for decision #7).
    """
    pushed_pipe = payload.get("data_pipe", "") or ""
    if pushed_pipe and pushed_pipe != current_pipe_name:
        # Should never happen — orchestrator overrides. Keep current.
        pushed_pipe = current_pipe_name

    browser = payload.get("browser") or {}

    raw_ext = browser.get("extensions") or []
    extensions = [
        e.lower() if e.startswith(".") else f".{e.lower()}"
        for e in raw_ext
    ]
    mime_types = [m.lower() for m in (browser.get("mime_types") or [])]
    domain_blocklist = [d.lower() for d in (browser.get("domain_blocklist") or [])]
    upload_url_keywords = [
        k.lower() for k in (browser.get("upload_url_keywords") or _DEFAULT_UPLOAD_KEYWORDS)
    ]

    return Config(
        pipe_name=pushed_pipe or current_pipe_name,
        timeout_seconds=float(browser.get("pipe_timeout_seconds", 5.0)),
        fail_behavior=browser.get("fail_behavior", "block"),
        temp_dir=browser.get("temp_dir", ""),
        min_upload_size_bytes=int(browser.get("min_upload_size_bytes", 1024)),
        extensions=extensions,
        mime_types=mime_types,
        domain_blocklist=domain_blocklist,
        upload_url_keywords=upload_url_keywords,
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
