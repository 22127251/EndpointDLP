"""Driver tests for orchestrator.installer (Phase D IT-D7).

These tests exercise the transactional rollback path with a synthetic step
list. They deliberately do NOT touch the real Win32 surface (no registry,
no certutil, no sc.exe, no admin elevation) — those code paths are verified
by the manual end-to-end smoke in the plan file.

What's covered here:
- Forward run with all steps succeeding -> manifest persisted, every undo NOT
  called.
- Forward run with step N raising -> undos for steps 0..N-1 fire in reverse,
  manifest is wiped, return code 1.
- run_uninstall with no manifest -> synthesizes default sweep + completes
  with return 0.
- run_uninstall idempotent -> second call is a no-op (undo handlers swallow
  "already absent" errors).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from orchestrator.config import OrchestratorConfig
from orchestrator.installer import (
    InstallContext,
    Step,
    _drive_install,
    _drive_uninstall,
    build_bundle_config,
)


def test_build_bundle_config_rewrites_for_vm(tmp_path: Path) -> None:
    src = tmp_path / "config.yaml"
    src.write_text(yaml.safe_dump({
        "data_pipe": "p", "ctl_pipe": "c",
        "paths": {
            "mitmdump_exe": "",
            "controller_exe": "interceptors/.../UsbDlpController.exe",
            "log_dir": "D:/somewhere/logs",
        },
        "policies_file": "analyzer/policies.yaml",
        "browser": {"temp_dir": r"D:\Code\GithubPublishEndpointDLP\tmp",
                    "pipe_timeout_seconds": 5},
        "install": {"install_root": "C:/old", "service_name": "DLPAgent"},
        "peripheral_storage": {"target_processes": ["explorer.exe"]},
    }), encoding="utf-8")
    dest = tmp_path / "bundle" / "config.yaml"

    build_bundle_config(src, dest)

    out = yaml.safe_load(dest.read_text(encoding="utf-8"))
    # paths rewritten to bundle/install layout
    assert out["paths"]["controller_exe"] == "bin/Controller/UsbDlpController.exe"
    assert out["paths"]["payload_dll"] == "bin/Controller/Payload.dll"
    assert out["paths"]["shell_extension_dll"] == "bin/ShellExt/DlpShellExt.dll"
    assert out["paths"]["mitmdump_exe"] == "python-embed/Scripts/mitmdump.exe"
    assert out["paths"]["log_dir"] == ""
    # host-absolute settings neutralized
    assert out["browser"]["temp_dir"] == ""
    assert out["install"]["install_root"] == ""
    # unrelated values copied verbatim
    assert out["browser"]["pipe_timeout_seconds"] == 5
    assert out["peripheral_storage"]["target_processes"] == ["explorer.exe"]
    assert out["data_pipe"] == "p"


# ─── Helpers ────────────────────────────────────────────────────────────────


def _minimal_context(tmp_path: Path) -> InstallContext:
    cfg = OrchestratorConfig(
        data_pipe="x", ctl_pipe="x",
        clipboard_workers=1, browser_workers=1, peripheral_storage_workers=1,
        pipe_listeners=1,
        max_clipboard_bytes=1, max_file_bytes=1,
        max_restarts=3, restart_window_seconds=60, stable_uptime_reset_seconds=60,
        mitmdump_exe="", addon_script="", clipboard_exe="", controller_exe="",
        log_dir=str(tmp_path / "logs"),
        proxy_listen_port=8080, proxy_bypass="",
        policies_file="",
        raw={"install": {}},
    )
    return InstallContext(
        config=cfg,
        config_path=tmp_path / "config.yaml",
        dev_root=tmp_path / "dev",
        install_root=tmp_path / "install",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        mitm_confdir=tmp_path / "mitm",
        service_name="DLPAgentTest",
        service_display="DLPAgent (test)",
        service_desc="test",
    )


def _record_step(name: str, log: list[str], fail: bool = False) -> Step:
    """Synthetic step: do logs 'do:<name>', undo logs 'undo:<name>'.
    When fail=True, do raises after logging so we can verify rollback."""
    def do(_ctx: InstallContext) -> dict[str, Any] | None:
        log.append(f"do:{name}")
        if fail:
            raise RuntimeError(f"{name} forced failure")
        return {"name": name}

    def undo(_ctx: InstallContext, payload: dict[str, Any] | None) -> None:
        recorded = (payload or {}).get("name", name)
        log.append(f"undo:{recorded}")
    return Step(name, do, undo)


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_install_all_steps_succeed_persists_manifest(tmp_path: Path) -> None:
    ctx = _minimal_context(tmp_path)
    events: list[str] = []
    steps = [_record_step(n, events) for n in ("alpha", "bravo", "charlie")]

    rc = _drive_install(ctx, steps)

    assert rc == 0
    assert events == ["do:alpha", "do:bravo", "do:charlie"]
    # Manifest persists every step's id + payload.
    assert ctx.manifest_path.exists()
    import json
    saved = json.loads(ctx.manifest_path.read_text(encoding="utf-8"))
    assert [e["id"] for e in saved] == ["alpha", "bravo", "charlie"]
    assert all(e["undo_payload"]["name"] in {"alpha", "bravo", "charlie"} for e in saved)


def test_install_midway_failure_rolls_back_in_reverse(tmp_path: Path) -> None:
    ctx = _minimal_context(tmp_path)
    events: list[str] = []
    steps = [
        _record_step("alpha", events),
        _record_step("bravo", events),
        _record_step("charlie", events, fail=True),
        _record_step("delta", events),
    ]

    rc = _drive_install(ctx, steps)

    assert rc == 1
    # alpha+bravo do; charlie do (raises); then rollback runs bravo+alpha undos.
    # charlie failed mid-do, so it never persisted to the manifest -> no undo.
    # delta never ran.
    assert events == ["do:alpha", "do:bravo", "do:charlie", "undo:bravo", "undo:alpha"]
    # Manifest deleted after rollback.
    assert not ctx.manifest_path.exists()


def test_uninstall_with_saved_manifest_runs_undo_in_reverse(tmp_path: Path) -> None:
    ctx = _minimal_context(tmp_path)
    events: list[str] = []
    steps = [_record_step(n, events) for n in ("alpha", "bravo", "charlie")]

    # First do a successful install so a manifest exists on disk.
    assert _drive_install(ctx, steps) == 0
    events.clear()

    # Fresh context simulates a separate uninstall invocation.
    ctx2 = _minimal_context(tmp_path)
    rc = _drive_uninstall(ctx2, steps)

    assert rc == 0
    assert events == ["undo:charlie", "undo:bravo", "undo:alpha"]
    assert not ctx2.manifest_path.exists()


def test_uninstall_without_manifest_synthesizes_default_sweep(tmp_path: Path) -> None:
    ctx = _minimal_context(tmp_path)
    events: list[str] = []
    steps = [_record_step(n, events) for n in ("alpha", "bravo", "charlie")]
    # No prior install -> no manifest on disk.
    assert not ctx.manifest_path.exists()

    rc = _drive_uninstall(ctx, steps)

    assert rc == 0
    # Synthesized sweep calls every undo in reverse with None payload (fallback
    # to step name via the helper). Order is the reverse of the step list.
    assert events == ["undo:charlie", "undo:bravo", "undo:alpha"]


def test_uninstall_idempotent_second_call_is_safe(tmp_path: Path) -> None:
    ctx = _minimal_context(tmp_path)
    events: list[str] = []

    def benign_undo(_ctx: InstallContext, _payload: dict[str, Any] | None) -> None:
        # Idempotent undo that survives being called twice.
        events.append("undo")

    def raise_already_absent(_ctx: InstallContext, _payload: dict[str, Any] | None) -> None:
        # Simulates a real "already absent" undo — driver must swallow this.
        raise FileNotFoundError("target already gone")

    steps = [
        Step("a", lambda c: None, benign_undo),
        Step("b", lambda c: None, raise_already_absent),
    ]

    rc1 = _drive_uninstall(ctx, steps)
    rc2 = _drive_uninstall(ctx, steps)
    assert rc1 == 0 and rc2 == 0
    # Each call invokes both undos; raise_already_absent is swallowed both
    # times, benign_undo records each call.
    assert events.count("undo") == 2


def test_install_returns_nonzero_on_failure_without_writing_manifest(tmp_path: Path) -> None:
    """First step failing -> nothing persisted, rollback is a no-op, return 1."""
    ctx = _minimal_context(tmp_path)
    events: list[str] = []
    steps = [_record_step("alpha", events, fail=True)]
    rc = _drive_install(ctx, steps)
    assert rc == 1
    assert events == ["do:alpha"]
    assert not ctx.manifest_path.exists()
