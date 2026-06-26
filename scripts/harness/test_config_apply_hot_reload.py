"""Layer A — exhaustive, in-process unit test of OrchestratorConfig.apply_hot_reload.

The fast, deterministic backbone of the config-reload coverage: every flat
OrchestratorConfig field is classified as either *owned by apply_hot_reload* (it
reloads live) or *not owned* (apply_hot_reload leaves it frozen — it is either
truly restart-only, or reloaded by a DIFFERENT mechanism such as the App Control
channel's apply_config). A meta-assertion enumerates the dataclass and fails if a
field is added without a reload decision, so the two lists below can never silently
drift from the code.

This layer imports only orchestrator.config (no analyzer deps), so it runs anywhere.
The end-to-end "the live agent actually changes behavior" proof is Layer B
(test_config_hot_reload_e2e.py); the client broadcast payload is Layer C
(test_ctl_pipe.py).
"""
from __future__ import annotations

import copy
import dataclasses

import pytest

from orchestrator.config import (
    OrchestratorConfig,
    _HOT_RELOADABLE_FIELDS,
    _config_from_raw,
)

# A complete baseline config dict (every section/key apply_hot_reload re-parses).
_BASE: dict = {
    "data_pipe": r"\\.\pipe\base_data",
    "ctl_pipe": r"\\.\pipe\base_ctl",
    "admin_pipe": r"\\.\pipe\base_admin",
    "pools": {
        "clipboard_workers": 2, "browser_workers": 3,
        "peripheral_storage_workers": 2, "pipe_listeners": 4,
    },
    "limits": {"max_file_bytes": 104857600},
    "analyzer": {
        "max_extracted_chars": 16000000,
        "supported_extensions": [".txt", ".pdf"],
    },
    "supervisor": {
        "max_restarts": 3, "restart_window_seconds": 60,
        "stable_uptime_reset_seconds": 60,
    },
    "service": {"drain_timeout_seconds": 12, "analysis_timeout_seconds": 10},
    "paths": {
        "mitmdump_exe": "m", "addon_script": "a", "clipboard_exe": "c",
        "controller_exe": "ctrl", "transfer_agent_exe": "ta",
        "shell_extension_dll": "se", "payload_dll": "pd", "log_dir": "ld",
    },
    "proxy": {"listen_port": 8080, "bypass": "localhost"},
    "policies_file": "analyzer/policies.yaml",
    "clipboard": {"pipe_timeout_ms": 12000, "failure_mode": "fail_closed"},
    "browser": {"pipe_timeout_ms": 12000, "failure_mode": "fail_closed"},
    "peripheral_storage": {
        "controller": {
            "failure_mode": "fail_closed", "target_processes": ["explorer.exe"],
            "shared_memory_name": "X", "payload_dll_path": "Payload.dll",
        },
        "transfer_agent": {
            "connect_timeout_ms": 5000, "analysis_timeout_ms": 12000,
            "failure_mode": "fail_closed",
        },
    },
    "app_control": {
        "enabled": True, "inbox_dir": "i", "rejected_dir": "r", "staging_dir": "s",
        "poll_seconds": 3, "reconcile_interval_seconds": 30,
        "forward_block_events": True, "extra_paths": [],
    },
    "server": {
        "url": "", "agent_id": "", "heartbeat_interval": 30,
        "log_sync_interval": 300, "enabled": False,
    },
}


def _base() -> dict:
    return copy.deepcopy(_BASE)


def _cfg() -> OrchestratorConfig:
    return _config_from_raw(_base())


# ── HOT: (field, raw-mutator, live-getter, expected-new-value) ───────────────
# One case per field in _HOT_RELOADABLE_FIELDS. failure_mode is a dict sourced
# from three channel sections; flipping clipboard's verifies the whole dict swaps.
_HOT_CASES = [
    ("failure_mode",
     lambda r: r["clipboard"].update(failure_mode="fail_open"),
     lambda c: c.failure_mode["clipboard"], "fail_open"),
    ("max_file_bytes",
     lambda r: r["limits"].update(max_file_bytes=123),
     lambda c: c.max_file_bytes, 123),
    ("max_extracted_chars",
     lambda r: r["analyzer"].update(max_extracted_chars=777),
     lambda c: c.max_extracted_chars, 777),
    ("supported_extensions",
     lambda r: r["analyzer"].update(supported_extensions=["zzz"]),
     lambda c: c.supported_extensions, [".zzz"]),   # normalized: lowercased + leading dot
    ("analysis_timeout_seconds",
     lambda r: r["service"].update(analysis_timeout_seconds=2),
     lambda c: c.analysis_timeout_seconds, 2.0),
    ("drain_timeout_seconds",
     lambda r: r["service"].update(drain_timeout_seconds=99),
     lambda c: c.drain_timeout_seconds, 99),
]


@pytest.mark.parametrize("name,mutate,get,expected", _HOT_CASES, ids=[c[0] for c in _HOT_CASES])
def test_hot_field_reloads(name, mutate, get, expected):
    cfg = _cfg()
    new_raw = _base()
    mutate(new_raw)
    changed = cfg.apply_hot_reload(new_raw)
    assert get(cfg) == expected, f"{name}: live value did not update"
    assert name in changed, f"{name}: not reported in changed={changed}"


# ── NOT-OWNED-BY-apply_hot_reload: (field, raw-mutator, live-getter) ─────────
# apply_hot_reload must leave each of these frozen on the live object.
def _set(path_keys, value):
    """Return a mutator that walks nested dict keys and sets the leaf."""
    def _mutate(r):
        d = r
        for k in path_keys[:-1]:
            d = d[k]
        d[path_keys[-1]] = value
    return _mutate


_NONRELOAD_CASES = [
    # truly restart-only (no running component re-reads them)
    ("data_pipe", _set(["data_pipe"], r"\\.\pipe\changed"), lambda c: c.data_pipe),
    ("ctl_pipe", _set(["ctl_pipe"], r"\\.\pipe\changed"), lambda c: c.ctl_pipe),
    ("admin_pipe", _set(["admin_pipe"], r"\\.\pipe\changed"), lambda c: c.admin_pipe),
    ("clipboard_workers", _set(["pools", "clipboard_workers"], 99), lambda c: c.clipboard_workers),
    ("browser_workers", _set(["pools", "browser_workers"], 99), lambda c: c.browser_workers),
    ("peripheral_storage_workers", _set(["pools", "peripheral_storage_workers"], 99),
     lambda c: c.peripheral_storage_workers),
    ("pipe_listeners", _set(["pools", "pipe_listeners"], 99), lambda c: c.pipe_listeners),
    ("mitmdump_exe", _set(["paths", "mitmdump_exe"], "CH"), lambda c: c.mitmdump_exe),
    ("addon_script", _set(["paths", "addon_script"], "CH"), lambda c: c.addon_script),
    ("clipboard_exe", _set(["paths", "clipboard_exe"], "CH"), lambda c: c.clipboard_exe),
    ("controller_exe", _set(["paths", "controller_exe"], "CH"), lambda c: c.controller_exe),
    ("transfer_agent_exe", _set(["paths", "transfer_agent_exe"], "CH"), lambda c: c.transfer_agent_exe),
    ("shell_extension_dll", _set(["paths", "shell_extension_dll"], "CH"), lambda c: c.shell_extension_dll),
    ("payload_dll", _set(["paths", "payload_dll"], "CH"), lambda c: c.payload_dll),
    ("log_dir", _set(["paths", "log_dir"], "CH"), lambda c: c.log_dir),
    ("proxy_listen_port", _set(["proxy", "listen_port"], 9999), lambda c: c.proxy_listen_port),
    ("proxy_bypass", _set(["proxy", "bypass"], "CH"), lambda c: c.proxy_bypass),
    ("policies_file", _set(["policies_file"], "CH"), lambda c: c.policies_file),
    ("max_restarts", _set(["supervisor", "max_restarts"], 99), lambda c: c.max_restarts),
    ("restart_window_seconds", _set(["supervisor", "restart_window_seconds"], 99),
     lambda c: c.restart_window_seconds),
    ("stable_uptime_reset_seconds", _set(["supervisor", "stable_uptime_reset_seconds"], 99),
     lambda c: c.stable_uptime_reset_seconds),
    # cloud bridge (server:) — restart-only. config.py marks these NOT hot-reloadable
    # (an agent_id change mid-flight would break heartbeat identity); CloudBridge reads
    # them once at startup in __main__.py.
    ("server_url", _set(["server", "url"], "http://changed:8000"), lambda c: c.server_url),
    ("server_agent_id", _set(["server", "agent_id"], "CH"), lambda c: c.server_agent_id),
    ("server_heartbeat_interval", _set(["server", "heartbeat_interval"], 99),
     lambda c: c.server_heartbeat_interval),
    ("server_log_sync_interval", _set(["server", "log_sync_interval"], 99),
     lambda c: c.server_log_sync_interval),
    ("server_enabled", _set(["server", "enabled"], True), lambda c: c.server_enabled),
    ("app_control_enabled", _set(["app_control", "enabled"], False), lambda c: c.app_control_enabled),
    ("app_control_inbox_dir", _set(["app_control", "inbox_dir"], "CH"), lambda c: c.app_control_inbox_dir),
    ("app_control_rejected_dir", _set(["app_control", "rejected_dir"], "CH"),
     lambda c: c.app_control_rejected_dir),
    ("app_control_staging_dir", _set(["app_control", "staging_dir"], "CH"),
     lambda c: c.app_control_staging_dir),
    ("app_control_reconcile_interval_seconds", _set(["app_control", "reconcile_interval_seconds"], 99),
     lambda c: c.app_control_reconcile_interval_seconds),
    ("app_control_extra_paths", _set(["app_control", "extra_paths"], ["CH"]),
     lambda c: c.app_control_extra_paths),
    # reloaded by the App Control channel's apply_config, NOT apply_hot_reload
    # (Layer A asserts only that apply_hot_reload leaves them; §7.4 covers their live reload)
    ("app_control_poll_seconds", _set(["app_control", "poll_seconds"], 99),
     lambda c: c.app_control_poll_seconds),
    ("app_control_forward_block_events", _set(["app_control", "forward_block_events"], False),
     lambda c: c.app_control_forward_block_events),
]


@pytest.mark.parametrize("name,mutate,get", _NONRELOAD_CASES, ids=[c[0] for c in _NONRELOAD_CASES])
def test_nonreload_field_frozen(name, mutate, get):
    cfg = _cfg()
    before = get(cfg)
    new_raw = _base()
    mutate(new_raw)
    changed = cfg.apply_hot_reload(new_raw)
    assert get(cfg) == before, f"{name}: changed on the live object but must be restart-only"
    assert name not in changed, f"{name}: wrongly reported in changed={changed}"


def test_pipe_names_never_in_changed_even_when_all_three_change():
    """Defense-in-depth: apply_hot_reload never reports a pipe-name change (the
    primary guard is __main__._handle_config_change overriding them first)."""
    cfg = _cfg()
    new_raw = _base()
    new_raw["data_pipe"] = r"\\.\pipe\d2"
    new_raw["ctl_pipe"] = r"\\.\pipe\c2"
    new_raw["admin_pipe"] = r"\\.\pipe\a2"
    changed = cfg.apply_hot_reload(new_raw)
    assert {"data_pipe", "ctl_pipe", "admin_pipe"}.isdisjoint(changed)
    assert cfg.data_pipe == _BASE["data_pipe"]


def test_every_flat_field_is_classified():
    """Meta-assertion: every flat OrchestratorConfig field (minus `raw`) is in
    exactly one of the HOT / NOT-OWNED lists, and HOT matches _HOT_RELOADABLE_FIELDS.
    Adding a field to the dataclass without classifying it fails here."""
    hot = {c[0] for c in _HOT_CASES}
    nonreload = {c[0] for c in _NONRELOAD_CASES}
    all_fields = {f.name for f in dataclasses.fields(OrchestratorConfig)} - {"raw"}
    assert hot.isdisjoint(nonreload), f"field in both lists: {hot & nonreload}"
    assert hot == set(_HOT_RELOADABLE_FIELDS), (
        f"_HOT_CASES {hot} != _HOT_RELOADABLE_FIELDS {set(_HOT_RELOADABLE_FIELDS)}")
    classified = hot | nonreload
    assert classified == all_fields, (
        f"unclassified fields: {all_fields - classified}; "
        f"names not on the dataclass: {classified - all_fields}")
