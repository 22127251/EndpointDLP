from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class OrchestratorConfig:
    data_pipe: str
    ctl_pipe: str
    clipboard_workers: int
    browser_workers: int
    peripheral_storage_workers: int
    pipe_listeners: int
    max_clipboard_bytes: int
    max_file_bytes: int
    max_restarts: int
    restart_window_seconds: int
    stable_uptime_reset_seconds: int
    mitmdump_exe: str
    addon_script: str
    clipboard_exe: str
    log_dir: str
    proxy_listen_port: int
    proxy_bypass: str
    policies_file: str


def load_config(path: str | Path | None = None) -> OrchestratorConfig:
    if path is None:
        path = Path(__file__).parent.parent / "orchestrator.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    pools = raw.get("pools", {})
    limits = raw.get("limits", {})
    supervisor = raw.get("supervisor", {})
    paths = raw.get("paths", {})
    proxy = raw.get("proxy", {})

    return OrchestratorConfig(
        data_pipe=raw["data_pipe"],
        ctl_pipe=raw["ctl_pipe"],
        clipboard_workers=pools.get("clipboard_workers", 2),
        browser_workers=pools.get("browser_workers", 3),
        peripheral_storage_workers=pools.get("peripheral_storage_workers", 2),
        pipe_listeners=pools.get("pipe_listeners", 4),
        max_clipboard_bytes=limits.get("max_clipboard_bytes", 1048576),
        max_file_bytes=limits.get("max_file_bytes", 104857600),
        max_restarts=supervisor.get("max_restarts", 3),
        restart_window_seconds=supervisor.get("restart_window_seconds", 60),
        stable_uptime_reset_seconds=supervisor.get("stable_uptime_reset_seconds", 60),
        mitmdump_exe=paths.get("mitmdump_exe", ""),
        addon_script=paths.get("addon_script", "interceptors/browser/addon.py"),
        clipboard_exe=paths.get("clipboard_exe", ""),
        log_dir=paths.get("log_dir", ""),
        proxy_listen_port=proxy.get("listen_port", 8080),
        proxy_bypass=proxy.get("bypass", "localhost;127.0.0.1;<local>"),
        policies_file=raw.get("policies_file", "analyzer/policies.yaml"),
    )
