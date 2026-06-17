from __future__ import annotations

from dataclasses import dataclass, field
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
    controller_exe: str
    log_dir: str
    proxy_listen_port: int
    proxy_bypass: str
    policies_file: str
    # Phase D additions — sourced from paths: in config.yaml. transfer_agent_exe also
    # backs the HKLM TransferAgentPath registry value the ShellExtension consults at
    # runtime; shell_extension_dll + payload_dll are install-time copy sources.
    # Defaulted to "" so existing test fixtures (test_supervisor.py:_minimal_config)
    # that predate Phase D don't need to enumerate them.
    transfer_agent_exe: str = ""
    shell_extension_dll: str = ""
    payload_dll: str = ""
    # Phase F additions. admin_pipe is the Administrators-only request/response
    # control channel for dlp-ctl (see admin_server.py). drain_timeout_seconds
    # bounds how long SvcStop waits for in-flight analyses before abandoning
    # them. Defaulted so pre-Phase-F fixtures that build the dataclass directly
    # don't need to enumerate them.
    admin_pipe: str = "\\\\.\\pipe\\dlp_agent_admin"
    drain_timeout_seconds: int = 8
    # How long the orchestrator waits for an analysis before failing closed.
    # Sourced from service.analysis_timeout_seconds; client pipe timeouts must
    # exceed this. Default 4.0 keeps the harness (no `service` section) unchanged.
    analysis_timeout_seconds: float = 4.0
    # Per-channel verdict when input exceeds the size cap ("block" = fail-closed,
    # the default; "allow" = fail-open). Sourced from limits.oversize_fail_behavior.
    oversize_fail_behavior: dict = field(default_factory=dict)
    # Phase AC-3 additions — the App Control (WDAC) channel. Sourced from the
    # app_control: section in config.yaml. Defaulted so pre-AC-3 fixtures that
    # build the dataclass directly (test_supervisor.py:_minimal_config) don't need
    # to enumerate them. Empty dir strings → resolved against %PROGRAMDATA%\DLP\
    # appcontrol\<sub> in channel.py (same derivation as installer/supervisor).
    app_control_enabled: bool = True
    app_control_inbox_dir: str = ""
    app_control_rejected_dir: str = ""
    app_control_staging_dir: str = ""
    app_control_poll_seconds: int = 3
    app_control_reconcile_interval_seconds: int = 30
    app_control_forward_block_events: bool = True
    app_control_extra_paths: list = field(default_factory=list)
    # Whole parsed yaml. Only the ctl-pipe broadcaster reads this — every other
    # orchestrator module reads the flat fields above. Keeping the raw tree lets
    # us project per-component sections (clipboard / browser / peripheral_storage)
    # over the ctl-pipe without re-parsing the file on every change.
    raw: dict = field(default_factory=dict)


def load_config(path: str | Path | None = None) -> OrchestratorConfig:
    if path is None:
        path = Path(__file__).parent.parent / "config.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    pools = raw.get("pools", {})
    limits = raw.get("limits", {})
    supervisor = raw.get("supervisor", {})
    paths = raw.get("paths", {})
    proxy = raw.get("proxy", {})
    service = raw.get("service", {})
    app_control = raw.get("app_control", {})

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
        controller_exe=paths.get(
            "controller_exe",
            "interceptors/peripheral_storage/Controller/bin/Debug/net10.0-windows/win-x64/UsbDlpController.exe",
        ),
        log_dir=paths.get("log_dir", ""),
        proxy_listen_port=proxy.get("listen_port", 8080),
        proxy_bypass=proxy.get("bypass", "localhost;127.0.0.1;<local>"),
        policies_file=raw.get("policies_file", "analyzer/policies.yaml"),
        transfer_agent_exe=paths.get(
            "transfer_agent_exe",
            "interceptors/peripheral_storage/TransferAgent/bin/Debug/net10.0-windows/win-x64/DlpTransferAgent.exe",
        ),
        shell_extension_dll=paths.get(
            "shell_extension_dll",
            "interceptors/peripheral_storage/out/ShellExtension/Debug/DlpShellExt.dll",
        ),
        payload_dll=paths.get(
            "payload_dll",
            "interceptors/peripheral_storage/Payload/x64/Debug/Payload.dll",
        ),
        admin_pipe=raw.get("admin_pipe", "\\\\.\\pipe\\dlp_agent_admin"),
        drain_timeout_seconds=service.get("drain_timeout_seconds", 8),
        analysis_timeout_seconds=float(service.get("analysis_timeout_seconds", 4.0)),
        oversize_fail_behavior=dict(limits.get("oversize_fail_behavior", {}) or {}),
        app_control_enabled=app_control.get("enabled", True),
        app_control_inbox_dir=app_control.get("inbox_dir", ""),
        app_control_rejected_dir=app_control.get("rejected_dir", ""),
        app_control_staging_dir=app_control.get("staging_dir", ""),
        app_control_poll_seconds=app_control.get("poll_seconds", 3),
        app_control_reconcile_interval_seconds=app_control.get("reconcile_interval_seconds", 30),
        app_control_forward_block_events=app_control.get("forward_block_events", True),
        app_control_extra_paths=list(app_control.get("extra_paths", []) or []),
        raw=raw,
    )
