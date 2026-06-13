"""Phase AC-4 — dlp-ctl App Control authoring workflow tests.

Covers the pure/in-process pieces: list management, the build pipeline (with the
PowerShell compile stubbed and PE-attribute/hash seams mocked), apply, the
``--force-local`` disable path, the generalized admin-pipe ``commands`` routing,
and ``channel.disable()``. No subprocess, no PowerShell, no real citool — the same
injectable-seam discipline as ``test_app_control_inbox.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.admin_server import AdminServer
from orchestrator.app_control import builder, paths
from orchestrator.app_control import hashing
from orchestrator.app_control import policy_xml as px
from orchestrator.app_control.channel import AppControlChannel
from orchestrator.config import load_config

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG = _REPO_ROOT / "config.yaml"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A real OrchestratorConfig whose appcontrol/state dirs resolve under tmp_path
    (PROGRAMDATA redirect — paths.py reads the env at call time, so every inbox /
    staging / status / list path is isolated)."""
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    return load_config(_CONFIG)


def _stub_compiler(xml_path, cip_path):
    """Stand in for ConvertFrom-CIPolicy: just emit a non-empty .cip."""
    Path(cip_path).write_bytes(b"FAKE-CIP-BINARY")


def _touch(p: Path, data: bytes = b"MZ") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _fixed_internalname(monkeypatch, value):
    monkeypatch.setattr(px, "read_file_attribute",
                        lambda path, level="InternalName": value)


# --------------------------------------------------------------------------- #
# list management
# --------------------------------------------------------------------------- #

def test_list_add_remove_dedup(tmp_path):
    lst = tmp_path / "allow-list.txt"
    added, all_entries = builder.add_entries(lst, ["C:/a.exe", "C:/dir"])
    assert added == ["C:/a.exe", "C:/dir"]
    assert all_entries == ["C:/a.exe", "C:/dir"]

    added2, _ = builder.add_entries(lst, ["C:/A.EXE"])  # case-insensitive dedup
    assert added2 == []

    removed, remaining = builder.remove_entries(lst, ["C:/DIR"])
    assert removed == ["C:/dir"] and remaining == ["C:/a.exe"]
    assert builder.read_entries(lst) == ["C:/a.exe"]


def test_read_entries_skips_comments_and_blanks(tmp_path):
    lst = tmp_path / "deny-list.txt"
    lst.write_text("# header\n\nC:/a.exe\n  C:/b.dll  \n# c\n", encoding="utf-8")
    assert builder.read_entries(lst) == ["C:/a.exe", "C:/b.dll"]


def test_collect_files_filters_recurses_and_errors(tmp_path):
    _touch(tmp_path / "a.exe")
    _touch(tmp_path / "b.dll")
    _touch(tmp_path / "c.txt")              # non-PE -> filtered out
    _touch(tmp_path / "sub" / "d.exe")
    files = builder.collect_files([str(tmp_path)])
    assert sorted(Path(f).name for f in files) == ["a.exe", "b.dll", "d.exe"]

    with pytest.raises(FileNotFoundError):
        builder.collect_files([str(tmp_path / "does-not-exist")])


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

def test_build_fileattrib_and_selfprotect(cfg, tmp_path, monkeypatch):
    app = _touch(tmp_path / "app.exe")
    builder.add_entries(paths.allow_list_path(cfg), [str(app)])
    _fixed_internalname(monkeypatch, "FakeApp")

    result = builder.build(cfg, compiler=_stub_compiler)
    build_dir = Path(result["staging_dir"])
    assert (build_dir / "policy.xml").is_file()
    assert (build_dir / f"{result['policy_id']}.cip").is_file()
    assert (build_dir / "manifest.json").is_file()
    assert result["version_ex"] == "10.3.0.1"     # base floor 10.3.0.0 bumped
    assert result["allow_files"] == 1 and result["deny_files"] == 0

    xml = (build_dir / "policy.xml").read_text(encoding="utf-8")
    assert 'InternalName="FakeApp"' in xml
    assert r"DLP\*" in xml          # install_root self-protect FilePath
    assert r"dotnet\*" in xml       # the mandatory dotnet carry-forward


def test_build_hash_fallback(cfg, tmp_path, monkeypatch):
    dll = _touch(tmp_path / "nometa.dll")
    builder.add_entries(paths.allow_list_path(cfg), [str(dll)])
    _fixed_internalname(monkeypatch, None)         # no PE version-info
    monkeypatch.setattr(hashing, "compute_wdac_hashes",
                        lambda f, **kw: ["AA11", "BB22", "CC33", "DD44"])

    result = builder.build(cfg, compiler=_stub_compiler)
    assert len(result["hashed"]) == 1
    xml = (Path(result["staging_dir"]) / "policy.xml").read_text(encoding="utf-8")
    assert 'Hash="AA11"' in xml and 'Hash="DD44"' in xml


def test_build_empty_lists_raises(cfg):
    with pytest.raises(builder.BuildError):
        builder.build(cfg, compiler=_stub_compiler)


def test_build_version_above_deployed_and_explicit(cfg, tmp_path, monkeypatch):
    status = paths.status_path(cfg)
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps({"policy_guid": "{x}", "version_ex": "10.9.0.5"}),
                      encoding="utf-8")
    app = _touch(tmp_path / "app.exe")
    builder.add_entries(paths.allow_list_path(cfg), [str(app)])
    _fixed_internalname(monkeypatch, "FakeApp")

    auto = builder.build(cfg, compiler=_stub_compiler)
    assert auto["version_ex"] == "10.9.0.6"        # bumped above deployed

    with pytest.raises(builder.BuildError):         # explicit <= deployed rejected
        builder.build(cfg, version="10.9.0.5", compiler=_stub_compiler)

    ok = builder.build(cfg, version="11.0.0.0", compiler=_stub_compiler)
    assert ok["version_ex"] == "11.0.0.0"


# --------------------------------------------------------------------------- #
# apply + disable_local
# --------------------------------------------------------------------------- #

def test_apply_moves_staging_to_inbox(cfg, tmp_path, monkeypatch):
    app = _touch(tmp_path / "app.exe")
    builder.add_entries(paths.allow_list_path(cfg), [str(app)])
    _fixed_internalname(monkeypatch, "FakeApp")
    builder.build(cfg, compiler=_stub_compiler)

    res = builder.apply(cfg)
    applied = Path(res["applied"])
    assert applied.parent == paths.inbox_dir(cfg)
    for name in ("policy.xml", "manifest.json"):
        assert (applied / name).is_file()
    assert any(applied.glob("*.cip"))
    # staging consumed
    assert not (paths.staging_dir(cfg) / "build" / "manifest.json").exists()

    with pytest.raises(builder.BuildError):         # nothing left to apply
        builder.apply(cfg)


def test_disable_local_drives_deployer(cfg, monkeypatch):
    captured: dict = {}

    class FakeDeployer:
        def __init__(self, **kw):
            captured.update(kw)

        def remove(self):
            captured["removed"] = True
            return True

        def read_status(self):
            return {"policy_guid": None, "last_error": None}

    monkeypatch.setattr(builder, "Deployer", FakeDeployer)
    res = builder.disable_local(cfg)
    assert res["removed"] is True and captured.get("removed") is True
    assert "policy_id" in captured and "status_path" in captured


# --------------------------------------------------------------------------- #
# admin-pipe commands dict + channel.disable
# --------------------------------------------------------------------------- #

def test_admin_commands_dict_routing():
    seen: dict = {}

    def disable_handler(req):
        seen["req"] = req
        return {"removed": True}

    srv = AdminServer(config=None,
                      status_provider=lambda: {"x": 1},
                      reload_callback=lambda: {"reloaded": []},
                      commands={"appcontrol_disable": disable_handler})
    r = srv.handle_request({"cmd": "appcontrol_disable", "foo": 1})
    assert r == {"ok": True, "removed": True}
    assert seen["req"]["foo"] == 1
    # status / reload unchanged
    assert srv.handle_request({"cmd": "status"})["x"] == 1
    assert srv.handle_request({"cmd": "reload"})["reloaded"] == []
    # unknown still errors
    u = srv.handle_request({"cmd": "nope"})
    assert u["ok"] is False and "unknown cmd" in u["error"]


def test_admin_without_commands_is_backward_compatible():
    srv = AdminServer(config=None, status_provider=lambda: {}, reload_callback=lambda: {})
    assert srv.handle_request({"cmd": "bogus"})["ok"] is False


def test_channel_disable(cfg):
    ch = AppControlChannel(cfg)
    assert ch.disable()["removed"] is False        # not started -> no deployer

    class FakeDeployer:
        def remove(self):
            return True

        def reconcile(self):
            return False

        def read_status(self):
            return {"policy_guid": None, "version_ex": None, "deployed_at": None,
                    "last_error": None, "blocks": {"enforce": 0, "audit": 0},
                    "last_block_at": None}

    ch._deployer = FakeDeployer()
    ch._started = True
    res = ch.disable()
    assert res["removed"] is True
    assert res["running"] is True and res["policy_guid"] is None


# --------------------------------------------------------------------------- #
# ctl arg parsing
# --------------------------------------------------------------------------- #

def test_ctl_appcontrol_requires_subcommand():
    import orchestrator.ctl as ctl
    with pytest.raises(SystemExit):
        ctl.main(["appcontrol"])
