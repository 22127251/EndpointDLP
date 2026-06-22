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

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.installer import (
    InstallContext,
    Step,
    _build_default_steps,
    _drive_install,
    _drive_uninstall,
    _step_appcontrol_dirs,
    _step_appcontrol_policy_guard,
    _step_enable_configci,
    _step_install_ctl_wrapper,
    _step_install_uninstall_wrapper,
    build_bundle_config,
)

# base.xml's PolicyID — what appcontrol_policy_guard records / removes.
_BASE_GUID = "{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}"


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
        "browser": {"pipe_timeout_ms": 12000},
        "install": {"install_root": "C:/old", "service_name": "DLPAgent"},
        "peripheral_storage": {"target_processes": ["explorer.exe"]},
        "app_control": {"enabled": True, "poll_seconds": 3},
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
    # host-absolute settings neutralized (browser.temp_dir is no longer a config
    # key — it is hardcoded to system %TEMP% in interceptors/browser/config.py)
    assert out["install"]["install_root"] == ""
    assert "temp_dir" not in out.get("browser", {})
    # unrelated values copied verbatim
    assert out["browser"]["pipe_timeout_ms"] == 12000
    assert out["peripheral_storage"]["target_processes"] == ["explorer.exe"]
    assert out["data_pipe"] == "p"
    # AC-5 (D5): the app_control section flows into the bundle untouched, so the
    # channel + ConfigCI-enable behave on the VM exactly as configured on dev.
    assert out["app_control"] == {"enabled": True, "poll_seconds": 3}


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


def test_install_ctl_wrapper_writes_and_undoes(tmp_path: Path) -> None:
    ctx = _minimal_context(tmp_path)
    ctx.install_root.mkdir(parents=True, exist_ok=True)
    step = _step_install_ctl_wrapper()

    payload = step.do(ctx)
    wrapper = ctx.install_root / "dlp-ctl.cmd"
    assert wrapper.exists()
    body = wrapper.read_text(encoding="ascii")
    assert "python\\python.exe" in body
    assert "-m orchestrator.ctl" in body

    step.undo(ctx, payload)
    assert not wrapper.exists()
    step.undo(ctx, payload)  # idempotent — no error when already gone


def test_install_uninstall_wrapper_writes_and_undoes(tmp_path: Path) -> None:
    ctx = _minimal_context(tmp_path)
    ctx.install_root.mkdir(parents=True, exist_ok=True)
    step = _step_install_uninstall_wrapper()

    payload = step.do(ctx)
    wrapper = ctx.install_root / "uninstall.cmd"
    assert wrapper.exists()
    body = wrapper.read_text(encoding="ascii")
    # Runs the INSTALLED python (WDAC-allowed), with the install root baked in
    # (not %~dp0, which becomes %TEMP% after the self-relaunch).
    assert f"{ctx.install_root}\\python\\python.exe" in body
    assert "-m orchestrator --uninstall" in body
    # Self-relaunch-from-%TEMP% markers so it never deletes itself mid-run.
    assert "_fromtemp" in body
    assert "%TEMP%\\dlp-uninstall.cmd" in body
    assert 'start "DLP Uninstall"' in body
    assert "{install_root}" not in body          # placeholder fully substituted

    step.undo(ctx, payload)
    assert not wrapper.exists()
    step.undo(ctx, payload)  # idempotent


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


# ─── AC-5: App Control installer steps ───────────────────────────────────────


def test_appcontrol_dirs_create_and_undo(tmp_path: Path, monkeypatch) -> None:
    """do creates the appcontrol root + inbox/rejected/staging; undo strips the
    WHOLE tree, including the operator's lists and any rejected pushes (decision 7)."""
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))   # paths.appcontrol_root -> tmp
    ctx = _minimal_context(tmp_path)
    step = _step_appcontrol_dirs()

    payload = step.do(ctx)
    root = tmp_path / "DLP" / "appcontrol"
    assert root.is_dir()
    for sub in ("inbox", "rejected", "staging"):
        assert (root / sub).is_dir()
    assert payload["root"] == str(root)

    # Simulate operator state the undo must also remove.
    (root / "allow-list.txt").write_text("C:\\Windows\\System32\\notepad.exe\n",
                                          encoding="utf-8")
    (root / "rejected" / "20260613T000000_bad").mkdir()

    step.undo(ctx, payload)
    assert not root.exists()
    step.undo(ctx, payload)            # idempotent — already gone, no error


def test_appcontrol_dirs_undo_recovers_root_from_config_when_payload_none(
        tmp_path: Path, monkeypatch) -> None:
    """A synthesized-sweep uninstall passes payload=None; undo must still find +
    remove the root by recomputing it from config."""
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    ctx = _minimal_context(tmp_path)
    step = _step_appcontrol_dirs()
    step.do(ctx)
    root = tmp_path / "DLP" / "appcontrol"
    assert root.is_dir()

    step.undo(ctx, None)
    assert not root.exists()


class _FakeDism:
    """Records each /add-package call. Returns a fixed rc, or per-call rcs (a list,
    last value repeating) so a mix of applicable/not-applicable packages can be
    simulated."""
    def __init__(self, rc: int = 0, rcs: list | None = None, out: str = "") -> None:
        self.rc, self.rcs, self.out, self.calls = rc, list(rcs) if rcs else None, out, []

    def __call__(self, mum_path: str):
        self.calls.append(mum_path)
        if self.rcs is not None:
            return self.rcs[min(len(self.calls) - 1, len(self.rcs) - 1)], self.out
        return self.rc, self.out


class _FakeProbe:
    """ConfigCI-availability probe stub. ``results`` is a list of bools consumed per
    call (last value repeats); records how many times it was called."""
    def __init__(self, results: list) -> None:
        self.results, self.calls = list(results), 0

    def __call__(self) -> bool:
        out = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return out


def _seed_configci_mums(pkg_dir: Path, n: int = 2) -> None:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (pkg_dir / f"Microsoft-Windows-ConfigCI-Package-{i}.mum").write_text("x",
                                                                             encoding="utf-8")
    # a non-matching package that must be ignored by the glob
    (pkg_dir / "Microsoft-Windows-Unrelated.mum").write_text("x", encoding="utf-8")


def test_enable_configci_success_continues_past_not_applicable(tmp_path: Path) -> None:
    """The real VM case: one .mum applies, another fails 'not applicable' (rc 14107);
    the post-loop probe says ConfigCI is now available -> success (no raise)."""
    pkg = tmp_path / "Packages"
    _seed_configci_mums(pkg, n=2)
    fake = _FakeDism(rcs=[14107, 0])                  # first down-level, second applies
    probe = _FakeProbe([False, True])                 # unavailable pre-loop, available after
    ctx = _minimal_context(tmp_path)
    step = _step_enable_configci(dism_runner=fake, packages_dir=pkg, probe=probe)

    payload = step.do(ctx)
    assert payload["packages_added"] == ["Microsoft-Windows-ConfigCI-Package-1.mum"]
    assert payload["packages_failed"] == ["Microsoft-Windows-ConfigCI-Package-0.mum"]
    assert len(fake.calls) == 2                       # every .mum attempted, no early raise


def test_enable_configci_treats_3010_as_added(tmp_path: Path) -> None:
    pkg = tmp_path / "Packages"
    _seed_configci_mums(pkg, n=1)
    probe = _FakeProbe([False, True])
    ctx = _minimal_context(tmp_path)
    step = _step_enable_configci(dism_runner=_FakeDism(rc=3010), packages_dir=pkg, probe=probe)
    payload = step.do(ctx)                            # no raise
    assert len(payload["packages_added"]) == 1


def test_enable_configci_idempotent_when_already_available(tmp_path: Path) -> None:
    pkg = tmp_path / "Packages"
    _seed_configci_mums(pkg, n=2)
    fake = _FakeDism(rc=0)
    probe = _FakeProbe([True])                        # already available
    ctx = _minimal_context(tmp_path)
    step = _step_enable_configci(dism_runner=fake, packages_dir=pkg, probe=probe)

    payload = step.do(ctx)
    assert payload == {"already_available": True}
    assert fake.calls == []                           # DISM loop skipped


def test_enable_configci_fail_closed_when_probe_never_succeeds(tmp_path: Path) -> None:
    """Every package fails AND ConfigCI never becomes available -> fail-closed raise."""
    pkg = tmp_path / "Packages"
    _seed_configci_mums(pkg, n=2)
    fake = _FakeDism(rc=14107)
    probe = _FakeProbe([False])                       # never available
    ctx = _minimal_context(tmp_path)
    step = _step_enable_configci(dism_runner=fake, packages_dir=pkg, probe=probe)
    with pytest.raises(RuntimeError, match="ConfigCI still unavailable"):
        step.do(ctx)
    assert len(fake.calls) == 2                       # attempted all before giving up


def test_enable_configci_fail_closed_when_no_packages(tmp_path: Path) -> None:
    pkg = tmp_path / "EmptyPackages"
    pkg.mkdir()
    fake = _FakeDism(rc=0)
    probe = _FakeProbe([False])                       # not available, nothing to add
    ctx = _minimal_context(tmp_path)
    step = _step_enable_configci(dism_runner=fake, packages_dir=pkg, probe=probe)
    with pytest.raises(RuntimeError, match="ConfigCI still unavailable"):
        step.do(ctx)
    assert fake.calls == []                           # no .mum -> runner never called


def test_enable_configci_skipped_when_app_control_disabled(tmp_path: Path) -> None:
    pkg = tmp_path / "Packages"
    _seed_configci_mums(pkg, n=2)
    fake = _FakeDism(rc=0)
    probe = _FakeProbe([False])
    ctx = _minimal_context(tmp_path)
    ctx.config.app_control_enabled = False           # the escape hatch (D1)
    step = _step_enable_configci(dism_runner=fake, packages_dir=pkg, probe=probe)

    payload = step.do(ctx)
    assert payload == {"skipped": True}
    assert fake.calls == []                           # DISM never invoked
    assert probe.calls == 0                           # returns before the probe


class _FakeCitool:
    """A citool runner stub: reports the policy gone (empty list) so
    deployer.remove() takes its success path without any real shell-out."""
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, args: list):
        self.calls.append(list(args))
        if args and args[0] == "--list-policies":
            return 0, '{"Policies":[]}'
        if args and args[0] == "--remove-policy":
            return 0, '{"OperationResult":0}'
        return 0, "{}"


def test_appcontrol_policy_guard_do_records_base_guid(tmp_path: Path) -> None:
    ctx = _minimal_context(tmp_path)
    payload = _step_appcontrol_policy_guard().do(ctx)
    assert payload == {"policy_id": _BASE_GUID}


def test_appcontrol_policy_guard_undo_removes_policy_and_clears_status(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))     # isolate any events.jsonl
    ctx = _minimal_context(tmp_path)
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    status = ctx.state_dir / "appcontrol_status.json"
    status.write_text('{"policy_guid": "x", "version_ex": "1.0.0.1"}', encoding="utf-8")

    fake = _FakeCitool()
    step = _step_appcontrol_policy_guard(citool_runner=fake)
    step.undo(ctx, {"policy_id": _BASE_GUID})

    assert any(c and c[0] == "--remove-policy" for c in fake.calls)
    assert not status.exists()                           # record cleared (D6)


def test_appcontrol_policy_guard_undo_resolves_guid_when_payload_none(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    ctx = _minimal_context(tmp_path)
    fake = _FakeCitool()
    step = _step_appcontrol_policy_guard(citool_runner=fake)
    step.undo(ctx, None)                                 # synthesized-sweep uninstall

    remove_calls = [c for c in fake.calls if c and c[0] == "--remove-policy"]
    assert remove_calls and remove_calls[0][1] == _BASE_GUID


def test_build_default_steps_orders_appcontrol_steps_correctly() -> None:
    ids = [s.id for s in _build_default_steps()]
    for new in ("appcontrol_dirs", "enable_configci", "appcontrol_policy_guard"):
        assert new in ids, f"{new} missing from default steps"
    # dirs + ConfigCI run before the heavier CA/proxy/service work (fail-fast, D3).
    assert ids.index("appcontrol_dirs") < ids.index("bootstrap_ca")
    assert ids.index("enable_configci") < ids.index("bootstrap_ca")
    # policy_guard undo must run after service-stop and before the install tree is
    # removed -> it sits after copy_payload and immediately before install_service (D3).
    assert ids.index("copy_payload") < ids.index("appcontrol_policy_guard")
    assert ids.index("appcontrol_policy_guard") == ids.index("install_service") - 1
    # The installed uninstaller is written right after the ctl wrapper.
    assert ids.index("install_uninstall_wrapper") == ids.index("install_ctl_wrapper") + 1
