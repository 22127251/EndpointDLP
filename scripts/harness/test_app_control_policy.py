"""Phase AC-2: pure-logic unit tests for the WDAC policy engine.

No subprocess, no real policy deploy. PE version-info reads and the hash shell-out
are exercised against real Windows binaries or mocked — the XML manipulation itself
is fully deterministic.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from orchestrator.app_control import SIPOLICY_NS
from orchestrator.app_control import hashing
from orchestrator.app_control import manifest as mf
from orchestrator.app_control import policy_xml as px
from orchestrator.app_control import selfprotect as sp

NS = SIPOLICY_NS


def q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _fresh():
    return px.load_base_policy()


def _file_rules(doc):
    return doc.getroot().find(q("FileRules"))


def _umci_refs(doc):
    frr = doc.getroot().find(
        f"{q('SigningScenarios')}/{q('SigningScenario')}[@Value='12']"
        f"/{q('ProductSigners')}/{q('FileRulesRef')}"
    )
    return [r.get("RuleID") for r in frr.findall(q("FileRuleRef"))] if frr is not None else []


# --------------------------------------------------------------------------- #
# T2 — load / serialize / version / PolicyInfo
# --------------------------------------------------------------------------- #

def test_load_base_policy_identity():
    doc = _fresh()
    assert px.get_version_ex(doc) == "10.3.0.0"
    assert px.get_policy_id(doc) == "{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}"


def test_serialize_roundtrip_default_namespace(tmp_path):
    doc = _fresh()
    out = tmp_path / "out.xml"
    px.serialize(doc, out)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<?xml")
    assert "ns0:" not in text  # default (unprefixed) namespace preserved
    assert 'xmlns="urn:schemas-microsoft-com:sipolicy"' in text
    reparsed = ET.parse(out)
    assert reparsed.getroot().tag == q("SiPolicy")
    assert px.get_version_ex(reparsed) == "10.3.0.0"


def test_set_and_bump_version():
    doc = _fresh()
    assert px.bump_version_ex(doc) == "10.3.0.1"
    assert px.set_version_ex(doc, "10.4.0.2") == "10.4.0.2"
    assert px.get_version_ex(doc) == "10.4.0.2"
    with pytest.raises(ValueError):
        px.set_version_ex(doc, "1.2.3")  # not 4-part


def test_set_policy_info_id():
    doc = _fresh()
    px.set_policy_info_id(doc, "2026-06-11")
    node = doc.getroot().find(
        f"{q('Settings')}/{q('Setting')}[@ValueName='Id']/{q('Value')}/{q('String')}"
    )
    assert node.text == "2026-06-11"


# --------------------------------------------------------------------------- #
# T3 — FileAttrib rules
# --------------------------------------------------------------------------- #

def test_add_file_attrib_allow_and_deny():
    doc = _fresh()
    a = px.add_file_attrib_rule(doc, "InternalName", "7zFM", allow=True)
    d = px.add_file_attrib_rule(doc, "InternalName", "olk", allow=False)
    assert a == "ID_ALLOW_A_1"
    assert d == "ID_DENY_D_1"
    fr = _file_rules(doc)
    assert fr.find(f"{q('Allow')}[@ID='ID_ALLOW_A_1']").get("InternalName") == "7zFM"
    assert fr.find(f"{q('Deny')}[@ID='ID_DENY_D_1']").get("InternalName") == "olk"
    refs = _umci_refs(doc)
    assert "ID_ALLOW_A_1" in refs and "ID_DENY_D_1" in refs


def test_file_attrib_dedup_and_numbering():
    doc = _fresh()
    a1 = px.add_file_attrib_rule(doc, "InternalName", "7zFM")
    a1_dup = px.add_file_attrib_rule(doc, "InternalName", "7zFM")
    a2 = px.add_file_attrib_rule(doc, "InternalName", "WinRAR")
    assert a1 == a1_dup == "ID_ALLOW_A_1"
    assert a2 == "ID_ALLOW_A_2"
    fr = _file_rules(doc)
    assert len([e for e in fr.findall(q("Allow")) if e.get("InternalName") == "7zFM"]) == 1
    assert _umci_refs(doc).count("ID_ALLOW_A_1") == 1


def test_original_filename_maps_to_wdac_filename_attr():
    doc = _fresh()
    rid = px.add_file_attrib_rule(doc, "OriginalFileName", "7zFM.exe")
    el = _file_rules(doc).find(f"{q('Allow')}[@ID='{rid}']")
    assert el.get("FileName") == "7zFM.exe"     # correct WDAC attribute
    assert el.get("OriginalFileName") is None    # not the literal level name


def test_add_file_attrib_rejects_empty_value():
    with pytest.raises(ValueError):
        px.add_file_attrib_rule(_fresh(), "InternalName", "")


def test_read_file_attribute_no_version_info(tmp_path):
    f = tmp_path / "plain.bin"
    f.write_bytes(b"not a PE file")
    assert px.read_file_attribute(f, "InternalName") is None


def test_read_file_attribute_real_binary():
    # exercises the real win32api path; kernel32 always carries a version resource.
    val = px.read_file_attribute(r"C:\Windows\System32\kernel32.dll", "ProductName")
    assert val and "Windows" in val


# --------------------------------------------------------------------------- #
# T4 — FilePath rules
# --------------------------------------------------------------------------- #

def test_add_filepath_rule():
    doc = _fresh()
    rid = px.add_filepath_rule(doc, r"C:\Program Files\DLP\*")
    el = _file_rules(doc).find(f"{q('Allow')}[@ID='{rid}']")
    assert el.get("FilePath") == r"C:\Program Files\DLP\*"
    assert el.get("MinimumFileVersion") == "0.0.0.0"
    assert rid in _umci_refs(doc)


def test_filepath_dedup():
    doc = _fresh()
    a = px.add_filepath_rule(doc, r"C:\Program Files\DLP\*")
    b = px.add_filepath_rule(doc, r"C:\Program Files\DLP\*")
    assert a == b
    assert len(_file_rules(doc).findall(q("Allow"))) == 1


# --------------------------------------------------------------------------- #
# T5 — Hash rules + risky-name warnings
# --------------------------------------------------------------------------- #

def test_add_hash_rules():
    doc = _fresh()
    hashes = ["AA11", "BB22", "CC33", "DD44"]
    ids = px.add_hash_rules(doc, "pcre2-8.dll", hashes)
    assert len(ids) == 4
    got = [e.get("Hash") for e in _file_rules(doc).findall(q("Allow"))]
    assert got == hashes
    refs = _umci_refs(doc)
    assert all(i in refs for i in ids)


def test_hash_dedup():
    doc = _fresh()
    px.add_hash_rules(doc, "x", ["AA11", "AA11"])
    assert len([e for e in _file_rules(doc).findall(q("Allow")) if e.get("Hash") == "AA11"]) == 1


def test_warn_on_risky_attribute():
    assert px.warn_on_risky_attribute("InternalName", "7zFM") == []
    assert px.warn_on_risky_attribute("InternalName", "Client Application")          # generic
    assert px.warn_on_risky_attribute("OriginalFileName", "7z2601-x64.exe")          # installer-like
    assert px.warn_on_risky_attribute("OriginalFileName", "setup.exe")               # installer-like


# --------------------------------------------------------------------------- #
# T6 — hashing.py (shell-out parsing, mocked runner)
# --------------------------------------------------------------------------- #

_HASH_POLICY_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<SiPolicy xmlns="urn:schemas-microsoft-com:sipolicy">
  <FileRules>
    <Allow ID="ID_ALLOW_A_0" FriendlyName="pcre2-8.dll Hash Sha1"      Hash="AA11" />
    <Allow ID="ID_ALLOW_A_1" FriendlyName="pcre2-8.dll Hash Sha256"    Hash="BB22" />
    <Allow ID="ID_ALLOW_A_2" FriendlyName="pcre2-8.dll Hash Page Sha1" Hash="CC33" />
    <Allow ID="ID_ALLOW_A_3" FriendlyName="pcre2-8.dll Hash Page 256"  Hash="DD44" />
  </FileRules>
</SiPolicy>"""


def test_compute_wdac_hashes_with_fake_runner(tmp_path):
    target = tmp_path / "pcre2-8.dll"
    target.write_bytes(b"x")
    out = hashing.compute_wdac_hashes(target, runner=lambda p: _HASH_POLICY_SAMPLE)
    assert out == ["AA11", "BB22", "CC33", "DD44"]


def test_extract_hashes_dedups():
    dup = _HASH_POLICY_SAMPLE.replace('Hash="DD44"', 'Hash="AA11"')
    assert hashing._extract_hashes(dup) == ["AA11", "BB22", "CC33"]


def test_compute_wdac_hashes_raises_when_no_rules():
    empty = '<SiPolicy xmlns="urn:schemas-microsoft-com:sipolicy"><FileRules/></SiPolicy>'
    with pytest.raises(RuntimeError):
        hashing.compute_wdac_hashes("anything", runner=lambda p: empty)


def test_hashes_feed_add_hash_rules():
    # the end-to-end seam: hashes from the (mocked) helper insert as 4 Allow rules.
    doc = _fresh()
    hashes = hashing.compute_wdac_hashes("f.dll", runner=lambda p: _HASH_POLICY_SAMPLE)
    ids = px.add_hash_rules(doc, "f.dll", hashes)
    assert len(ids) == 4
    got = [e.get("Hash") for e in _file_rules(doc).findall(q("Allow"))]
    assert got == ["AA11", "BB22", "CC33", "DD44"]


# --------------------------------------------------------------------------- #
# T7 — selfprotect.py (FilePath coverage + validator)
# --------------------------------------------------------------------------- #

INSTALL_ROOT = r"C:\Program Files\DLP"
DOTNET_ROOT = r"C:\Program Files\dotnet"


def test_default_dotnet_root_under_program_files(monkeypatch):
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    assert sp.default_dotnet_root() == r"C:\Program Files\dotnet"


def test_required_filepaths():
    assert sp.required_filepaths(INSTALL_ROOT, dotnet_root=DOTNET_ROOT) == [
        r"C:\Program Files\DLP\*",
        r"C:\Program Files\dotnet\*",
    ]


def test_add_and_validate_selfprotect():
    doc = _fresh()
    ids = sp.add_selfprotect_rules(doc, INSTALL_ROOT, dotnet_root=DOTNET_ROOT)
    assert len(ids) == 2
    assert sp.policy_covers_required_paths(doc, INSTALL_ROOT, dotnet_root=DOTNET_ROOT) is True
    assert sp.missing_required_paths(doc, INSTALL_ROOT, dotnet_root=DOTNET_ROOT) == []


def test_validate_fails_without_any_selfprotect():
    doc = _fresh()
    assert sp.policy_covers_required_paths(doc, INSTALL_ROOT, dotnet_root=DOTNET_ROOT) is False


def test_validate_fails_missing_dotnet():
    # install_root covered but the .NET runtime rule absent -> not covered.
    doc = _fresh()
    px.add_filepath_rule(doc, r"C:\Program Files\DLP\*")
    assert sp.policy_covers_required_paths(doc, INSTALL_ROOT, dotnet_root=DOTNET_ROOT) is False
    assert sp.missing_required_paths(doc, INSTALL_ROOT, dotnet_root=DOTNET_ROOT) == [
        r"C:\Program Files\dotnet\*"
    ]


def test_validate_fails_wrong_root():
    doc = _fresh()
    sp.add_selfprotect_rules(doc, r"C:\Program Files\OTHER", dotnet_root=DOTNET_ROOT)
    assert sp.policy_covers_required_paths(doc, INSTALL_ROOT, dotnet_root=DOTNET_ROOT) is False


def test_validate_requires_umci_reference():
    # an Allow FilePath rule present but NOT referenced in UMCI does not count.
    doc = _fresh()
    sp.add_selfprotect_rules(doc, INSTALL_ROOT, dotnet_root=DOTNET_ROOT)
    assert sp.policy_covers_required_paths(doc, INSTALL_ROOT, dotnet_root=DOTNET_ROOT)
    frr = doc.getroot().find(
        f"{q('SigningScenarios')}/{q('SigningScenario')}[@Value='12']"
        f"/{q('ProductSigners')}/{q('FileRulesRef')}"
    )
    frr.remove(frr.findall(q("FileRuleRef"))[0])  # strip one ref
    assert sp.policy_covers_required_paths(doc, INSTALL_ROOT, dotnet_root=DOTNET_ROOT) is False


# --------------------------------------------------------------------------- #
# T8 — manifest.py (schema + validator suite)
# --------------------------------------------------------------------------- #

GUID = "{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}"


def _make_inbox(tmp_path, *, with_selfprotect=True, version="10.4.0.2"):
    """Write a valid {policy.xml, {GUID}.cip} inbox + matching manifest dict."""
    doc = _fresh()
    px.set_version_ex(doc, version)
    if with_selfprotect:
        sp.add_selfprotect_rules(doc, INSTALL_ROOT, dotnet_root=DOTNET_ROOT)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    xml_path = inbox / "policy.xml"
    px.serialize(doc, xml_path)
    cip_path = inbox / f"{GUID}.cip"
    cip_path.write_bytes(b"\x00BINARY-CIP-PLACEHOLDER\x00")
    man = {
        "schema_version": 1,
        "policy_id": GUID,
        "version_ex": version,
        "created": "2026-06-11T00:00:00Z",
        "source": "standalone",
        "files": {
            "policy_xml": {"name": "policy.xml", "sha256": mf.flat_sha256(xml_path)},
            "cip": {"name": f"{GUID}.cip", "sha256": mf.flat_sha256(cip_path)},
        },
    }
    return inbox, man


def _validate(inbox, man, *, deployed="10.4.0.1"):
    return mf.validate_all(mf.parse_manifest(man), inbox,
                           deployed_version_ex=deployed,
                           install_root=INSTALL_ROOT, dotnet_root=DOTNET_ROOT)


def test_manifest_happy_path(tmp_path):
    inbox, man = _make_inbox(tmp_path)
    assert _validate(inbox, man) == []


def test_manifest_hash_mismatch(tmp_path):
    inbox, man = _make_inbox(tmp_path)
    man["files"]["cip"]["sha256"] = "deadbeef"
    assert any(f.code == "hash_mismatch" for f in _validate(inbox, man, deployed=None))


def test_manifest_cip_name_mismatch(tmp_path):
    inbox, man = _make_inbox(tmp_path)
    (inbox / f"{GUID}.cip").rename(inbox / "wrong.cip")
    man["files"]["cip"]["name"] = "wrong.cip"
    assert any(f.code == "cip_name_mismatch" for f in _validate(inbox, man, deployed=None))


def test_manifest_stale_version(tmp_path):
    inbox, man = _make_inbox(tmp_path, version="10.4.0.2")
    assert any(f.code == "stale_version" for f in _validate(inbox, man, deployed="10.4.0.2"))


def test_manifest_equal_version_is_stale(tmp_path):
    inbox, man = _make_inbox(tmp_path, version="10.4.0.5")
    assert any(f.code == "stale_version" for f in _validate(inbox, man, deployed="10.4.0.5"))


def test_manifest_missing_selfprotect(tmp_path):
    inbox, man = _make_inbox(tmp_path, with_selfprotect=False)
    assert any(f.code == "selfprotect_uncovered" for f in _validate(inbox, man, deployed=None))


def test_manifest_no_deployed_version_accepts(tmp_path):
    inbox, man = _make_inbox(tmp_path, version="10.3.0.0")
    assert _validate(inbox, man, deployed=None) == []


def test_parse_manifest_strict():
    with pytest.raises(mf.ManifestError):
        mf.parse_manifest({"schema_version": 1})        # missing fields
    with pytest.raises(mf.ManifestError):
        mf.parse_manifest("this is not json")
    with pytest.raises(mf.ManifestError):
        mf.parse_manifest(12345)                         # wrong type
