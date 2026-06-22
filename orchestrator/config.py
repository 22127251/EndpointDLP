from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Default analyzer-supported file extensions (lowercase, leading dot): the 8
# tested formats + clearly-textual fallbacks. See supported_extensions below.
_DEFAULT_SUPPORTED_EXTENSIONS = [
    ".docx", ".odt", ".ods", ".xlsx", ".csv", ".tsv",
    ".txt", ".md", ".pdf", ".json", ".yaml", ".yml", ".log",
]


def _normalize_extensions(raw_list) -> list:
    """Normalize a configured extension list to lowercase, leading-dot form
    (so ``XLSX`` / ``.XLSX`` / ``xlsx`` all match). Falls back to the default
    list when the config omits the key or supplies an empty/non-list value."""
    if not isinstance(raw_list, list) or not raw_list:
        return list(_DEFAULT_SUPPORTED_EXTENSIONS)
    out: list[str] = []
    for ext in raw_list:
        e = str(ext).strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        if e not in out:
            out.append(e)
    return out or list(_DEFAULT_SUPPORTED_EXTENSIONS)


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
    # Unified per-channel verdict for ANY orchestrator-side analysis failure
    # (oversize input, analysis timeout, analysis error — and the extracted-text
    # cap added in Phase 5): "fail_closed" → BLOCK (default), "fail_open" → ALLOW.
    # Sourced from <channel>.failure_mode; peripheral_storage reads the nested
    # transfer_agent.failure_mode (the component that owns the verdict). Use
    # verdict_for() rather than reading this dict directly.
    failure_mode: dict = field(default_factory=dict)
    # Phase 5: cap on extracted text per file analysis (characters; ~2 bytes each
    # in memory). Extraction is refused once the running count exceeds this
    # (ExtractionTooLarge → verdict_for(channel), reason=text_cap), bounding
    # per-analysis memory AND time. Sourced from analyzer.max_extracted_chars.
    # ~16M chars ≈ a few hundred MB peak/analysis; <=0 disables the cap.
    max_extracted_chars: int = 16_000_000
    # Allow-list of file extensions the analyzer will extract + scan (lowercase,
    # leading dot). A file whose extension is NOT here is refused BEFORE
    # extraction and follows the channel's failure_mode (reason=unsupported_format)
    # — so an untested/binary type (e.g. .exe, .jpg, .pptx) is never scanned as
    # garbage text. Sourced from analyzer.supported_extensions; applied at service
    # start (like max_extracted_chars). Only the file channels (browser /
    # peripheral_storage) consult it; clipboard text has no extension.
    supported_extensions: list = field(
        default_factory=lambda: list(_DEFAULT_SUPPORTED_EXTENSIONS))
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

    def verdict_for(self, channel: str) -> str:
        """Verdict ("BLOCK" | "ALLOW") for any orchestrator-side analysis failure
        on *channel* — the single point that maps `failure_mode` to a decision.
        fail_closed → BLOCK (the default when unset/unknown); fail_open → ALLOW."""
        mode = (self.failure_mode or {}).get(channel, "fail_closed")
        return "ALLOW" if mode == "fail_open" else "BLOCK"


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
    analyzer = raw.get("analyzer", {}) or {}
    app_control = raw.get("app_control", {})

    # Unified per-channel failure_mode. Each channel reads it from its own section;
    # peripheral_storage's lives on the transfer_agent subtree (that component owns
    # the verdict the orchestrator uses for peripheral analysis failures).
    clipboard_cfg = raw.get("clipboard", {}) or {}
    browser_cfg = raw.get("browser", {}) or {}
    peripheral_cfg = raw.get("peripheral_storage", {}) or {}
    transfer_agent_cfg = peripheral_cfg.get("transfer_agent", {}) or {}
    failure_mode = {
        "clipboard": clipboard_cfg.get("failure_mode", "fail_closed"),
        "browser": browser_cfg.get("failure_mode", "fail_closed"),
        "peripheral_storage": transfer_agent_cfg.get("failure_mode", "fail_closed"),
    }

    return OrchestratorConfig(
        data_pipe=raw["data_pipe"],
        ctl_pipe=raw["ctl_pipe"],
        clipboard_workers=pools.get("clipboard_workers", 2),
        browser_workers=pools.get("browser_workers", 3),
        peripheral_storage_workers=pools.get("peripheral_storage_workers", 2),
        pipe_listeners=pools.get("pipe_listeners", 4),
        # Phase 7: the clipboard text cap moved to clipboard.max_input_bytes (the
        # section the ClipboardInterceptor reads). Fall back to the old
        # limits.max_clipboard_bytes so pre-Phase-7 fixtures/configs still parse.
        max_clipboard_bytes=clipboard_cfg.get(
            "max_input_bytes", limits.get("max_clipboard_bytes", 8388608)),
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
        failure_mode=failure_mode,
        max_extracted_chars=analyzer.get("max_extracted_chars", 16_000_000),
        supported_extensions=_normalize_extensions(analyzer.get("supported_extensions")),
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
