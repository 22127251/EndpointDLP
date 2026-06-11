"""SiPolicy (WDAC) XML rule engine — pure Python port of ``Add-WDACRule.ps1``.

Loads a WDAC base policy, inserts Allow/Deny **file-attribute**, **FilePath**, and
**Hash** rules into ``<FileRules>`` plus a matching ``<FileRuleRef>`` into the UMCI
``<SigningScenario Value="12">``, bumps ``VersionEx``, stamps the PolicyInfo ``Id``,
and serializes back out. No OS side effects — ``read_file_attribute`` is the only
function that touches a file (reads its PE version-info header).

Parity notes vs ``Add-WDACRule.ps1``:
  - Rule IDs follow the same scheme: ``ID_ALLOW_A_<n>`` / ``ID_DENY_D_<n>``,
    auto-numbered above the highest existing index.
  - File-attribute rules are emitted as ``<Allow InternalName="..."/>`` style
    elements (the shape the WDAC Wizard emits for "File Attribute" rules), proven
    to compile + enforce on the AC-1 VM.
  - Improvement over the PS1: the ``OriginalFileName`` *level* maps to the correct
    WDAC XML attribute ``FileName`` (the PS1 emitted a literal ``OriginalFileName``
    attribute, which is not in the sipolicy schema). The version-resource key is
    likewise ``OriginalFilename`` (single-word), which is what the PE header uses.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

from . import BASE_POLICY_PATH, SIPOLICY_NS

log = logging.getLogger(__name__)

# Emit the sipolicy namespace as the default (unprefixed) one, matching base.xml.
ET.register_namespace("", SIPOLICY_NS)

#: UMCI (user-mode code integrity) signing-scenario Value.
_UMCI_VALUE = "12"

#: Public file-attribute level -> (PE version-resource StringFileInfo key,
#: WDAC sipolicy XML attribute name). Only InternalName/FileDescription/ProductName
#: share a name across all three; OriginalFileName differs on both sides.
_LEVELS = {
    "InternalName":     ("InternalName",     "InternalName"),
    "OriginalFileName": ("OriginalFilename", "FileName"),
    "FileDescription":  ("FileDescription",  "FileDescription"),
    "ProductName":      ("ProductName",      "ProductName"),
}

_ALLOW_ID_RE = re.compile(r"^ID_ALLOW_A_(\d+)$")
_DENY_ID_RE = re.compile(r"^ID_DENY_D_(\d+)$")


def _q(tag: str) -> str:
    """Clark-notation qualified tag in the sipolicy namespace."""
    return f"{{{SIPOLICY_NS}}}{tag}"


def _root(doc) -> ET.Element:
    return doc.getroot() if isinstance(doc, ET.ElementTree) else doc


# --------------------------------------------------------------------------- #
# Load / serialize
# --------------------------------------------------------------------------- #

def load_base_policy() -> ET.ElementTree:
    """Parse the packaged ``base.xml`` into a fresh ElementTree."""
    return ET.parse(BASE_POLICY_PATH)


def load_policy(path: str | Path) -> ET.ElementTree:
    """Parse an arbitrary SiPolicy XML file."""
    return ET.parse(path)


def serialize(doc, path: str | Path) -> None:
    """Write ``doc`` to ``path`` as indented UTF-8 with an XML declaration and the
    sipolicy namespace as the default (unprefixed) namespace."""
    root = _root(doc)
    if root.tag != _q("SiPolicy"):
        raise ValueError(f"root is not <SiPolicy> (got {root.tag})")
    tree = doc if isinstance(doc, ET.ElementTree) else ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------- #
# VersionEx / PolicyInfo / identity
# --------------------------------------------------------------------------- #

def _version_node(root: ET.Element) -> ET.Element:
    el = root.find(_q("VersionEx"))
    if el is None:
        raise ValueError("<VersionEx> not found")
    return el


def _parse_version(text: str) -> list[int]:
    parts = (text or "").strip().split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        raise ValueError(f"VersionEx must be 4 dotted integers, got {text!r}")
    return [int(p) for p in parts]


def get_version_ex(doc) -> str:
    return ".".join(str(p) for p in _parse_version(_version_node(_root(doc)).text))


def set_version_ex(doc, version: str) -> str:
    """Set ``<VersionEx>`` to ``version`` (validated 4-part dotted integer)."""
    parts = _parse_version(version if isinstance(version, str) else str(version))
    node = _version_node(_root(doc))
    node.text = ".".join(str(p) for p in parts)
    return node.text


def bump_version_ex(doc) -> str:
    """Increment the 4th field of ``<VersionEx>`` (PS1 parity)."""
    node = _version_node(_root(doc))
    parts = _parse_version(node.text)
    parts[3] += 1
    node.text = ".".join(str(p) for p in parts)
    return node.text


def get_policy_id(doc) -> str | None:
    el = _root(doc).find(_q("PolicyID"))
    return el.text.strip() if el is not None and el.text else None


def set_policy_info_id(doc, value: str) -> None:
    """Set ``Settings/Setting[@ValueName='Id']/Value/String`` (PS1 parity — the
    PowerShell tool stamps today's date here; the builder stamps the version)."""
    root = _root(doc)
    node = root.find(
        f"{_q('Settings')}/{_q('Setting')}[@ValueName='Id']/{_q('Value')}/{_q('String')}"
    )
    if node is None:
        raise ValueError("PolicyInfo <Setting ValueName='Id'> String not found")
    node.text = value


# --------------------------------------------------------------------------- #
# FileRules / FileRuleRef plumbing (shared by all rule types)
# --------------------------------------------------------------------------- #

def _file_rules(root: ET.Element) -> ET.Element:
    fr = root.find(_q("FileRules"))
    if fr is None:
        raise ValueError("<FileRules> not found in policy")
    return fr


def _umci_scenario(root: ET.Element) -> ET.Element:
    for sc in root.findall(f"{_q('SigningScenarios')}/{_q('SigningScenario')}"):
        if sc.get("Value") == _UMCI_VALUE:
            return sc
    raise ValueError(f'UMCI <SigningScenario Value="{_UMCI_VALUE}"> not found')


def _umci_file_rules_ref(root: ET.Element) -> ET.Element:
    """Return the UMCI ProductSigners/FileRulesRef, creating either if absent.
    FileRulesRef is appended after AllowedSigners (schema-correct, PS1 parity)."""
    umci = _umci_scenario(root)
    ps = umci.find(_q("ProductSigners"))
    if ps is None:
        ps = ET.SubElement(umci, _q("ProductSigners"))
    frr = ps.find(_q("FileRulesRef"))
    if frr is None:
        frr = ET.SubElement(ps, _q("FileRulesRef"))
    return frr


def _next_id(root: ET.Element, allow: bool) -> str:
    """Allocate the next ``ID_ALLOW_A_<n>`` / ``ID_DENY_D_<n>`` above the highest
    existing index (re-scans each call, so loops stay correct after inserts)."""
    pattern = _ALLOW_ID_RE if allow else _DENY_ID_RE
    highest = 0
    for el in root.iter():
        rid = el.get("ID")
        if rid:
            m = pattern.match(rid)
            if m:
                highest = max(highest, int(m.group(1)))
    prefix = "ID_ALLOW_A_" if allow else "ID_DENY_D_"
    return f"{prefix}{highest + 1}"


def _find_existing(file_rules: ET.Element, allow: bool, dedup: dict[str, str]):
    tag = _q("Allow") if allow else _q("Deny")
    for el in file_rules.findall(tag):
        if all(el.get(k) == v for k, v in dedup.items()):
            return el
    return None


def _ensure_ref(root: ET.Element, rule_id: str) -> None:
    frr = _umci_file_rules_ref(root)
    for ref in frr.findall(_q("FileRuleRef")):
        if ref.get("RuleID") == rule_id:
            return
    ET.SubElement(frr, _q("FileRuleRef")).set("RuleID", rule_id)


def _add_file_rule(root: ET.Element, allow: bool, attrs: dict[str, str],
                   friendly: str, dedup_attrs: Iterable[str] | None = None) -> str:
    """Insert one ``<Allow>``/``<Deny>`` FileRule (deduped on ``dedup_attrs``,
    default = all attrs) and wire its FileRuleRef into the UMCI scenario.
    Returns the rule ID (existing one if a dedup match was found)."""
    file_rules = _file_rules(root)
    dedup = {k: attrs[k] for k in (dedup_attrs or attrs.keys())}
    existing = _find_existing(file_rules, allow, dedup)
    if existing is not None:
        rid = existing.get("ID")
        _ensure_ref(root, rid)
        return rid
    rid = _next_id(root, allow)
    el = ET.SubElement(file_rules, _q("Allow") if allow else _q("Deny"))
    el.set("ID", rid)
    el.set("FriendlyName", friendly)
    for k, v in attrs.items():
        el.set(k, v)
    _ensure_ref(root, rid)
    return rid


# --------------------------------------------------------------------------- #
# Rule builders
# --------------------------------------------------------------------------- #

def read_file_attribute(path: str | Path, level: str = "InternalName") -> str | None:
    """Read a PE version-info string attribute (the pywin32 equivalent of the PS1's
    ``[System.Diagnostics.FileVersionInfo]``). Returns ``None`` when the file has no
    version resource or the attribute is empty — the signal to use the hash fallback.

    ``win32api`` is imported lazily so the rest of this module stays importable
    without pywin32 and unit tests can mock this function."""
    if level not in _LEVELS:
        raise ValueError(f"unknown level {level!r}; expected one of {list(_LEVELS)}")
    resource_key, _ = _LEVELS[level]
    import win32api  # lazy: Windows-only, mocked in tests

    try:
        translation = win32api.GetFileVersionInfo(str(path), "\\VarFileInfo\\Translation")
    except Exception:
        return None
    if not translation:
        return None
    lang, codepage = translation[0]
    key = "\\StringFileInfo\\%04X%04X\\%s" % (lang, codepage, resource_key)
    try:
        value = win32api.GetFileVersionInfo(str(path), key)
    except Exception:
        return None
    if not value:
        return None
    value = value.replace("\x00", "").strip()
    return value or None


def add_file_attrib_rule(doc, level: str, value: str, *, allow: bool = True) -> str:
    """Add a file-attribute Allow/Deny rule (e.g. ``InternalName="7zFM"``)."""
    if level not in _LEVELS:
        raise ValueError(f"unknown level {level!r}; expected one of {list(_LEVELS)}")
    if not value:
        raise ValueError(f"empty {level} value; cannot build a file-attribute rule")
    _, xml_attr = _LEVELS[level]
    verb = "Allow" if allow else "Deny"
    friendly = f"{verb} files based on file attributes: {value}"
    return _add_file_rule(root=_root(doc), allow=allow,
                          attrs={xml_attr: value}, friendly=friendly,
                          dedup_attrs=(xml_attr,))


def add_filepath_rule(doc, filepath: str, *, allow: bool = True) -> str:
    """Add a FilePath Allow/Deny rule (e.g. ``FilePath="C:\\Program Files\\DLP\\*"``).
    A trailing ``*`` authorizes all EXE/DLL in the path and subdirectories
    recursively. Honored only for admin-only-writable paths (no option 18 set)."""
    if not filepath:
        raise ValueError("empty FilePath")
    verb = "Allow" if allow else "Deny"
    friendly = f"{verb} files by path: {filepath}"
    return _add_file_rule(root=_root(doc), allow=allow,
                          attrs={"FilePath": filepath, "MinimumFileVersion": "0.0.0.0"},
                          friendly=friendly, dedup_attrs=("FilePath",))


def add_hash_rules(doc, friendly_name: str, hashes: Iterable[str], *,
                   allow: bool = True) -> list[str]:
    """Add one Allow/Deny rule per hash value (WDAC emits four hashes/file:
    SHA1/SHA256 Authenticode + SHA1/SHA256 page hash). The values are supplied by
    the caller (see ``hashing.compute_wdac_hashes``); this only inserts the XML."""
    verb = "Allow" if allow else "Deny"
    ids: list[str] = []
    for h in hashes:
        if not h:
            continue
        friendly = f"{verb} by hash: {friendly_name}"
        ids.append(_add_file_rule(root=_root(doc), allow=allow,
                                  attrs={"Hash": h}, friendly=friendly,
                                  dedup_attrs=("Hash",)))
    return ids


# --------------------------------------------------------------------------- #
# Risky-name warnings (decision 8b + AC-1 generic-InternalName hazard)
# --------------------------------------------------------------------------- #

# Installer-like: contains setup/install, ends with an arch suffix, or carries a
# 3+ digit run (e.g. "7z2601-x64") — an Allow on the installer won't cover the
# installed app, and a Deny may be over-broad.
_INSTALLER_RE = re.compile(
    r"(?:^|[^a-z])(setup|install|installer|update)(?:$|[^a-z])"
    r"|[-_ ](x64|x86|amd64|win32|win64)\b"
    r"|\d{3,}",
    re.IGNORECASE,
)

# Overly-generic values that would match unrelated binaries (AC-1: OneDrive's
# InternalName is literally "Client Application").
_GENERIC_VALUES = {
    "client application", "application", "app", "setup", "installer",
    "host", "service", "module", "library", "console", "tool",
}


def warn_on_risky_attribute(level: str, value: str) -> list[str]:
    """Return (and log at WARNING) any risk notes for a file-attribute value:
    installer-like names and dangerously-generic values."""
    notes: list[str] = []
    if not value:
        return notes
    v = value.strip()
    if _INSTALLER_RE.search(v):
        notes.append(
            f"{level} {value!r} looks installer-like: an Allow may not cover the "
            f"installed app (installer vs installed-app mismatch), and a Deny may be over-broad"
        )
    if v.lower() in _GENERIC_VALUES:
        notes.append(
            f"{level} {value!r} is generic and may match unrelated binaries "
            f"(e.g. OneDrive's InternalName is 'Client Application')"
        )
    for n in notes:
        log.warning(n)
    return notes
