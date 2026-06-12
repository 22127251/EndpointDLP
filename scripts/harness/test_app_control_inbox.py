"""Phase AC-3 unit tests — inbox watcher, citool deployer, event forwarder, and
the app_control events emitter.

Pure-logic + injected-fake style (mirrors test_events.py / test_app_control_policy.py):
no real citool, no real EvtSubscribe, no real PE files. The deployer's citool
subprocess is replaced by a fake ``runner``; the inbox watcher gets a fake deployer;
the forwarder's pure parser is fed a captured AC-1 3077 event XML.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import xml.etree.ElementTree as ET

from orchestrator import events
from orchestrator.app_control import SIPOLICY_NS
from orchestrator.app_control import manifest as mf
from orchestrator.app_control import neutralizer as nz
from orchestrator.app_control import policy_xml as px
from orchestrator.app_control import selfprotect as sp
from orchestrator.app_control.deployer import Deployer
from orchestrator.app_control.event_forwarder import build_xpath, parse_block_event
from orchestrator.app_control.inbox import InboxWatcher

GUID = "{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}"
BARE = GUID.strip("{}").lower()
INSTALL = r"C:\Program Files\DLP"
DOTNET = r"C:\Program Files\dotnet"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVENTS_DIR = _REPO_ROOT / "interceptors" / "app_control" / "spike-results" / "events"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def events_capture():
    logger = logging.getLogger("dlp.events")
    records: list[dict] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(json.loads(record.getMessage()))

    handler = _Cap()
    prev_level, prev_prop = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.propagate = prev_prop


def _make_push(root: Path, name: str, *, version: str = "10.5.0.1",
               with_selfprotect: bool = True, policy_id: str = GUID,
               corrupt_hash: bool = False, cip_name: str | None = None) -> Path:
    """Build a real push subfolder {policy.xml, {GUID}.cip, manifest.json}."""
    sub = root / name
    sub.mkdir(parents=True)
    doc = px.load_base_policy()
    px.set_version_ex(doc, version)
    px.add_file_attrib_rule(doc, "InternalName", "olk", allow=False)
    if with_selfprotect:
        sp.add_selfprotect_rules(doc, INSTALL, dotnet_root=DOTNET)
    pxml = sub / "policy.xml"
    px.serialize(doc, pxml)
    cip_name = cip_name or f"{policy_id}.cip"
    cip = sub / cip_name
    cip.write_bytes(b"FAKECIP-" + name.encode())
    man = {
        "schema_version": 1, "policy_id": policy_id, "version_ex": version,
        "created": "now", "source": "test",
        "files": {
            "policy_xml": {"name": "policy.xml", "sha256": mf.flat_sha256(pxml)},
            "cip": {"name": cip_name,
                    "sha256": "deadbeef" if corrupt_hash else mf.flat_sha256(cip)},
        },
    }
    (sub / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    return sub


def _manifest_for(version: str = "10.5.0.1") -> mf.Manifest:
    return mf.Manifest(1, GUID, version, "now", "test",
                       mf.FileEntry("policy.xml", "x"), mf.FileEntry(f"{GUID}.cip", "y"))


class _FakeDeployer:
    def __init__(self, *, deploy_ok: bool = True, deployed_ver: str | None = None):
        self.deploy_ok = deploy_ok
        self._ver = deployed_ver
        self.deploys: list = []

    def deployed_version_ex(self):
        return self._ver

    def deploy(self, sub, m):
        self.deploys.append(m.version_ex)
        return self.deploy_ok


# --------------------------------------------------------------------------- #
# Deployer (fake citool runner)
# --------------------------------------------------------------------------- #

def _runner_happy(args):
    if "--refresh" in args:
        return 0, json.dumps({"OperationResult": 0})
    if "--list-policies" in args:
        return 0, json.dumps({"Policies": [
            {"PolicyID": BARE, "VersionString": "10.5.0.1", "IsEnforced": True}]})
    return 0, "{}"


def _new_deployer(tmp_path, runner):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    active = tmp_path / "Active"
    active.mkdir(parents=True, exist_ok=True)
    neut = tmp_path / "neutralizer.cip"
    neut.write_bytes(b"NEUTRAL")
    status = tmp_path / "state" / "appcontrol_status.json"
    return Deployer(status_path=status, policy_id=GUID, runner=runner,
                    active_dir=active, neutralizer_cip=neut), active


def test_deploy_happy_persists_status(tmp_path, events_capture):
    d, active = _new_deployer(tmp_path, _runner_happy)
    push = tmp_path / "push"
    push.mkdir()
    (push / f"{GUID}.cip").write_bytes(b"CIP")
    assert d.deploy(push, _manifest_for("10.5.0.1")) is True
    assert (active / f"{GUID}.cip").is_file()
    st = d.read_status()
    assert st["policy_guid"] == GUID and st["version_ex"] == "10.5.0.1"
    assert any(e["event"] == "deploy" and e["outcome"] == "ok" for e in events_capture)


def test_deploy_refresh_fail_keeps_prior_status(tmp_path):
    # Seed a prior good deploy.
    d, active = _new_deployer(tmp_path, _runner_happy)
    push = tmp_path / "push"
    push.mkdir()
    (push / f"{GUID}.cip").write_bytes(b"GOOD")
    d.deploy(push, _manifest_for("10.5.0.1"))

    # Now a refresh-failing runner: prior status + on-disk cip must be preserved.
    def _runner_fail(args):
        if "--refresh" in args:
            return 5, json.dumps({"OperationResult": -2147024891})
        if "--list-policies" in args:
            return 0, json.dumps({"Policies": [{"PolicyID": BARE,
                                                "VersionString": "10.5.0.1"}]})
        return 0, "{}"
    d2 = Deployer(status_path=d._status_path, policy_id=GUID, runner=_runner_fail,
                  active_dir=active, neutralizer_cip=tmp_path / "neutralizer.cip")
    push2 = tmp_path / "push2"
    push2.mkdir()
    (push2 / f"{GUID}.cip").write_bytes(b"NEW")
    assert d2.deploy(push2, _manifest_for("10.6.0.1")) is False
    st = d2.read_status()
    assert st["version_ex"] == "10.5.0.1" and st["last_error"]
    assert (active / f"{GUID}.cip").read_bytes() == b"GOOD"  # rolled back


def test_remove_ok(tmp_path, events_capture):
    state = {"removed": False}

    def _runner(args):
        if "--remove-policy" in args:
            state["removed"] = True
            return 0, json.dumps({"OperationResult": 0})
        if "--list-policies" in args:
            pols = [] if state["removed"] else [{"PolicyID": BARE}]
            return 0, json.dumps({"Policies": pols})
        return 0, "{}"
    d, active = _new_deployer(tmp_path, _runner)
    (active / f"{GUID}.cip").write_bytes(b"ENFORCE")
    assert d.remove() is True
    assert not (active / f"{GUID}.cip").is_file()
    assert d.read_status()["policy_guid"] is None
    assert any(e["event"] == "remove" and e["outcome"] == "ok" for e in events_capture)


def test_remove_fail_falls_back_to_neutralizer(tmp_path, events_capture):
    def _runner(args):
        if "--remove-policy" in args:
            return 5, json.dumps({"OperationResult": -1})
        if "--refresh" in args:
            return 0, json.dumps({"OperationResult": 0})
        if "--list-policies" in args:
            return 0, json.dumps({"Policies": [{"PolicyID": BARE}]})  # never removed
        return 0, "{}"
    d, active = _new_deployer(tmp_path, _runner)
    (active / f"{GUID}.cip").write_bytes(b"ENFORCE")
    assert d.remove() is True
    assert not (active / f"{GUID}.cip").is_file()  # neutralizer applied then deleted
    assert any(e["event"] == "neutralize" for e in events_capture)


def test_note_block_counters(tmp_path):
    d, _ = _new_deployer(tmp_path, _runner_happy)
    d.note_block("blocked", {"file": "olk.exe", "process": "svchost.exe"})
    d.note_block("blocked", {"file": "OneDrive.exe"})
    d.note_block("audit", {"file": "x.dll"})
    b = d.read_status()["blocks"]
    assert b["enforce"] == 2 and b["audit"] == 1
    assert d.read_status()["last_block"]["file"] == "x.dll"


def test_deployer_reconcile_clears_and_adopts(tmp_path, events_capture):
    # (a) recorded policy no longer live → cleared, block counters reset.
    d, _ = _new_deployer(tmp_path, lambda a: (0, json.dumps({"Policies": []})))
    d._write_status({
        "policy_guid": GUID, "version_ex": "10.7.0.1", "deployed_at": "t",
        "last_error": None, "last_error_at": None, "blocks": {"enforce": 3, "audit": 0},
        "last_block_at": "t", "last_block": {"file": "olk.exe"}})
    assert d.reconcile() is True
    st = d.read_status()
    assert st["policy_guid"] is None and st["blocks"] == {"enforce": 0, "audit": 0}
    assert any(e["event"] == "reconcile" and e["outcome"] == "cleared" for e in events_capture)
    assert d.reconcile() is False   # idempotent

    # (b) live policy at a new version → status adopts it.
    d2, _ = _new_deployer(
        tmp_path / "b",
        lambda a: (0, json.dumps({"Policies": [{"PolicyID": BARE,
                                                "VersionString": "10.9.0.2"}]})))
    assert d2.reconcile() is True
    assert d2.read_status()["version_ex"] == "10.9.0.2"
    assert any(e["event"] == "reconcile" and e["outcome"] == "adopted" for e in events_capture)


# --------------------------------------------------------------------------- #
# Inbox watcher (fake deployer + real pushes)
# --------------------------------------------------------------------------- #

def _watcher(tmp_path, deployer, **kw):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    rejected = tmp_path / "rejected"
    return InboxWatcher(inbox_dir=inbox, rejected_dir=rejected, deployer=deployer,
                        base_policy_id=GUID, install_root=INSTALL, dotnet_root=DOTNET,
                        poll_seconds=0.01, **kw), inbox, rejected


def test_inbox_happy_deploys_after_stable(tmp_path, events_capture):
    fd = _FakeDeployer()
    w, inbox, _ = _watcher(tmp_path, fd)
    _make_push(inbox, "good")
    w.poll_once()                       # first sight — record snapshot, not ready
    assert fd.deploys == []
    w.poll_once()                       # stable — deploy
    assert fd.deploys == ["10.5.0.1"]
    assert not (inbox / "good").exists()  # consumed


@pytest.mark.parametrize("kw,expect_code", [
    (dict(corrupt_hash=True), "hash_mismatch"),
    (dict(with_selfprotect=False), "selfprotect_uncovered"),
    (dict(policy_id="{11111111-1111-1111-1111-111111111111}"), "foreign_policy_id"),
    (dict(cip_name="wrong.cip"), "cip_name_mismatch"),
])
def test_inbox_rejections(tmp_path, events_capture, kw, expect_code):
    fd = _FakeDeployer()
    w, inbox, rejected = _watcher(tmp_path, fd)
    _make_push(inbox, "bad", **kw)
    w.poll_once()
    w.poll_once()
    assert fd.deploys == []
    codes = [f["code"] for e in events_capture if e["event"] == "reject"
             for f in e["failures"]]
    assert expect_code in codes
    assert rejected.is_dir() and any(rejected.iterdir())
    assert w.rejected_count == 1


def test_inbox_stale_version_rejected(tmp_path, events_capture):
    fd = _FakeDeployer(deployed_ver="10.9.0.0")   # already higher than the push
    w, inbox, _ = _watcher(tmp_path, fd)
    _make_push(inbox, "stale", version="10.5.0.1")
    w.poll_once()
    w.poll_once()
    codes = [f["code"] for e in events_capture if e["event"] == "reject"
             for f in e["failures"]]
    assert "stale_version" in codes and fd.deploys == []


def test_inbox_bad_json_rejected(tmp_path, events_capture):
    fd = _FakeDeployer()
    w, inbox, rejected = _watcher(tmp_path, fd)
    sub = inbox / "garbage"
    sub.mkdir()
    (sub / "manifest.json").write_text("}{ not json", encoding="utf-8")
    w.poll_once()
    w.poll_once()
    codes = [f["code"] for e in events_capture if e["event"] == "reject"
             for f in e["failures"]]
    assert "manifest_parse" in codes


def test_inbox_manifest_absent_is_skipped(tmp_path):
    fd = _FakeDeployer()
    w, inbox, rejected = _watcher(tmp_path, fd)
    sub = inbox / "partial"
    sub.mkdir()
    (sub / "policy.xml").write_text("<x/>", encoding="utf-8")   # no manifest yet
    w.poll_once()
    w.poll_once()
    assert fd.deploys == []
    assert (inbox / "partial").exists()       # left in place; not rejected
    assert not (rejected.exists() and any(rejected.iterdir()))


def test_inbox_deploy_failure_quarantines(tmp_path, events_capture):
    fd = _FakeDeployer(deploy_ok=False)
    w, inbox, rejected = _watcher(tmp_path, fd)
    _make_push(inbox, "willfail")
    w.poll_once()
    w.poll_once()
    assert fd.deploys == ["10.5.0.1"]          # deploy attempted
    assert not (inbox / "willfail").exists()    # moved out of inbox
    assert rejected.is_dir() and any(rejected.iterdir())


# --------------------------------------------------------------------------- #
# Event forwarder (pure parser on a captured AC-1 3077)
# --------------------------------------------------------------------------- #

def _sample_3077() -> str:
    samples = sorted(_EVENTS_DIR.glob("*_3077_*.xml"))
    if not samples:
        pytest.skip("no captured 3077 sample available")
    return samples[0].read_text(encoding="utf-8")


def test_forwarder_parses_our_3077():
    outcome, detail = parse_block_event(_sample_3077(), BARE)
    assert outcome == "blocked"
    assert detail["internal_name"] == "olk"
    assert detail["file"].lower().endswith("olk.exe")
    assert detail["policy_guid"].strip("{}").lower() == BARE


def test_forwarder_audit_twin():
    xml = _sample_3077().replace("<EventID>3077</EventID>", "<EventID>3076</EventID>")
    outcome, _ = parse_block_event(xml, BARE)
    assert outcome == "audit"


def test_forwarder_drops_foreign_policy():
    assert parse_block_event(_sample_3077(), "11111111-1111-1111-1111-111111111111") is None


def test_forwarder_ignores_non_block_id():
    xml = _sample_3077().replace("<EventID>3077</EventID>", "<EventID>3033</EventID>")
    assert parse_block_event(xml, BARE) is None


def test_forwarder_swallows_malformed():
    assert parse_block_event("<not xml", BARE) is None
    assert parse_block_event("", BARE) is None


def test_forwarder_xpath():
    assert build_xpath((3076, 3077)) == "*[System[(EventID=3076 or EventID=3077)]]"
    assert build_xpath(()) == "*"


# --------------------------------------------------------------------------- #
# Events emitter
# --------------------------------------------------------------------------- #

def test_record_app_control_event_shape(events_capture):
    events.record_app_control_event(event="deploy", outcome="ok",
                                    detail={"policy_guid": GUID, "version_ex": "10.5.0.1"})
    events.record_app_control_event(event="block", outcome="blocked",
                                    detail={"file": "olk.exe"})
    rec = events_capture[0]
    assert rec["channel"] == "app_control" and rec["event"] == "deploy"
    assert rec["outcome"] == "ok" and rec["version_ex"] == "10.5.0.1" and "ts" in rec
    assert events_capture[1]["event"] == "block" and events_capture[1]["file"] == "olk.exe"


# --------------------------------------------------------------------------- #
# Neutralizer (builder + committed artifact)
# --------------------------------------------------------------------------- #

_MINI_ALLOWALL = """<?xml version="1.0" encoding="utf-8"?>
<SiPolicy xmlns="urn:schemas-microsoft-com:sipolicy" PolicyType="Base Policy">
  <VersionEx>1.0.0.0</VersionEx>
  <PolicyID>{00000000-0000-0000-0000-000000000000}</PolicyID>
  <BasePolicyID>{00000000-0000-0000-0000-000000000000}</BasePolicyID>
  <Rules><Rule><Option>Enabled:UMCI</Option></Rule></Rules>
  <FileRules><Allow ID="ID_ALLOW_A_1" FileName="*" /></FileRules>
  <SigningScenarios /><Settings />
</SiPolicy>
"""


def _q(tag):
    return f"{{{SIPOLICY_NS}}}{tag}"


def test_neutralizer_builder_restamps(tmp_path):
    template = tmp_path / "AllowAll.xml"
    template.write_text(_MINI_ALLOWALL, encoding="utf-8")
    doc = nz.build_neutralizer_doc(GUID, template_path=template)
    root = doc.getroot()
    assert root.findtext(_q("PolicyID")) == GUID
    assert root.findtext(_q("BasePolicyID")) == GUID
    assert root.findtext(_q("VersionEx")) == nz.NEUTRALIZER_VERSION_EX == "65535.65535.65535.65535"
    # AllowAll FileName="*" rule survives the restamp.
    allows = root.findall(f"{_q('FileRules')}/{_q('Allow')}")
    assert any(a.get("FileName") == "*" for a in allows)


def test_committed_neutralizer_is_current():
    doc = px.load_policy(nz.NEUTRALIZER_XML_PATH)
    assert px.get_policy_id(doc) == GUID
    assert px.get_version_ex(doc) == nz.NEUTRALIZER_VERSION_EX
    cip = nz.neutralizer_cip_path()
    assert cip.is_file() and cip.stat().st_size > 0
