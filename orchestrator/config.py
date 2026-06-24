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

# Hard safety ceiling for the clipboard data-pipe message when the extracted-text
# cap is disabled (analyzer.max_extracted_chars <= 0). The Windows clipboard is
# otherwise memory-bound only (no hard size limit), so we still refuse a pipe
# message beyond this to bound per-analysis memory. See clipboard_pipe_ceiling_bytes.
_CLIPBOARD_UNCAPPED_CEILING_BYTES = 256 * 1024 * 1024

# Flat OrchestratorConfig fields that take effect on a live config reload
# (dlp-ctl reload / config.yaml save) — see apply_hot_reload. Every OTHER flat
# field is restart-only (pipe names, worker pools, paths, proxy, policies_file,
# supervisor.*, app_control.* — the channel hot-reloads its own poll/forward via
# AppControlChannel.apply_config, NOT here). Keep in sync with the config-reload
# classification test (scripts/harness/test_config_apply_hot_reload.py).
_HOT_RELOADABLE_FIELDS = (
    "failure_mode",
    "max_file_bytes",
    "max_extracted_chars",
    "supported_extensions",
    "analysis_timeout_seconds",
    "drain_timeout_seconds",
)


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

    def clipboard_pipe_ceiling_bytes(self) -> int:
        """Max bytes the data-pipe will reassemble for one (clipboard) message.
        Derived from max_extracted_chars so it tracks hot-reloads: UTF-8 is at
        most 4 bytes/char, + 1 MB headroom for the JSON envelope/escaping. With
        the char-cap disabled (<=0), fall back to a fixed 256 MB safety bound
        (the clipboard is otherwise memory-bound only). A message past the ceiling
        is dropped by the server, so the client fails per its own failure_mode."""
        cap = self.max_extracted_chars
        if cap and cap > 0:
            return cap * 4 + (1 << 20)
        return _CLIPBOARD_UNCAPPED_CEILING_BYTES

    def apply_hot_reload(self, new_raw: dict) -> list[str]:
        """Re-apply the hot-reloadable fields (``_HOT_RELOADABLE_FIELDS``) from
        *new_raw* onto THIS object in place — every consumer (PolicyManager /
        Dispatcher / PipeServer) holds this instance by reference, so the in-place
        swap is what makes a config reload take effect without a restart. Every
        other (restart-only) field is left untouched. Returns the names of the
        fields whose value actually changed (for logging).

        Each assignment is a single attribute/reference set, which is atomic under
        the GIL, so a worker thread reading concurrently sees either the old or the
        new value — never a torn one. No extra lock is needed (matches the existing
        lock-free reads in Dispatcher/PolicyManager)."""
        fresh = _config_from_raw(new_raw)
        changed: list[str] = []
        for name in _HOT_RELOADABLE_FIELDS:
            if getattr(self, name) != getattr(fresh, name):
                setattr(self, name, getattr(fresh, name))
                changed.append(name)
        self.raw = new_raw
        return changed


def load_config(path: str | Path | None = None) -> OrchestratorConfig:
    if path is None:
        path = Path(__file__).parent.parent / "config.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return _config_from_raw(raw)


def _config_from_raw(raw: dict) -> OrchestratorConfig:
    """Build an OrchestratorConfig from an already-parsed config dict. Shared by
    load_config (file path) and apply_hot_reload (the watcher's new dict) so both
    parse identically."""
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
