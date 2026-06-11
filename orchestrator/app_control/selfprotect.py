"""Agent self-protection — the WDAC rules that keep the DLP agent runnable under
its own enforced policy, plus the validator the channel uses to reject a pushed
policy that doesn't cover the agent (parent decision 3).

Self-protect is built from **FilePath** rules on admin-only-writable directories:
  - ``<install_root>\*``        — the whole agent tree (embed Python + all native
                                  analyzer wheels + the C# interceptor exes + native
                                  DLLs). One recursive rule, no per-file hashing.
  - ``C:\Program Files\dotnet\*`` — the .NET 10 shared runtime the framework-dependent
                                  C# interceptors load. A DefaultWindows-style base
                                  (which ``base.xml`` is) does NOT trust .NET (it is
                                  signed Microsoft-Code-Signing-PCA, not the Windows
                                  EKU), so this explicit rule is required.

Both live under ``%ProgramFiles%`` (admin-only-writable), so the rules are honored
without the weaker ``Disabled:Runtime FilePath Rule Protection`` option.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

from . import SIPOLICY_NS
from . import policy_xml as px

_UMCI_VALUE = "12"


def _q(tag: str) -> str:
    return f"{{{SIPOLICY_NS}}}{tag}"


def _root(doc) -> ET.Element:
    return doc.getroot() if isinstance(doc, ET.ElementTree) else doc


def _norm(path: str | Path) -> str:
    """Normalize a Windows path for comparison: backslashes, no trailing separator."""
    return str(path).replace("/", "\\").rstrip("\\")


def _wildcard(path: str | Path) -> str:
    """The recursive FilePath wildcard for a directory (``...\\*``)."""
    return _norm(path) + r"\*"


def default_dotnet_root() -> str:
    """The .NET shared-runtime install dir (``%ProgramFiles%\\dotnet``)."""
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    return str(Path(program_files) / "dotnet")


def required_filepaths(install_root: str | Path, *, dotnet_root: str | Path | None = None,
                       extra_paths: list[str | Path] | None = None) -> list[str]:
    """The ordered, de-duplicated list of FilePath wildcards the agent needs allowed.
    Defaults to ``[<install_root>\\*, %ProgramFiles%\\dotnet\\*]``; ``extra_paths``
    lets AC-4/AC-5 extend it via config."""
    roots: list[str | Path] = [install_root, dotnet_root or default_dotnet_root()]
    if extra_paths:
        roots.extend(extra_paths)
    out: list[str] = []
    seen: set[str] = set()
    for r in roots:
        w = _wildcard(r)
        key = w.lower()
        if key not in seen:
            seen.add(key)
            out.append(w)
    return out


def add_selfprotect_rules(doc, install_root: str | Path, *,
                          dotnet_root: str | Path | None = None,
                          extra_paths: list[str | Path] | None = None) -> list[str]:
    """Add an Allow FilePath rule for every required path. The standalone builder
    (AC-4) always calls this before compiling."""
    return [
        px.add_filepath_rule(doc, w, allow=True)
        for w in required_filepaths(install_root, dotnet_root=dotnet_root, extra_paths=extra_paths)
    ]


def _umci_referenced_ids(root: ET.Element) -> set[str]:
    frr = root.find(
        f"{_q('SigningScenarios')}/{_q('SigningScenario')}[@Value='{_UMCI_VALUE}']"
        f"/{_q('ProductSigners')}/{_q('FileRulesRef')}"
    )
    if frr is None:
        return set()
    return {ref.get("RuleID") for ref in frr.findall(_q("FileRuleRef"))}


def _covered_filepaths(root: ET.Element) -> set[str]:
    """Lower-cased FilePath values of Allow rules that are also referenced in UMCI."""
    file_rules = root.find(_q("FileRules"))
    if file_rules is None:
        return set()
    referenced = _umci_referenced_ids(root)
    covered: set[str] = set()
    for allow in file_rules.findall(_q("Allow")):
        fp = allow.get("FilePath")
        if fp and allow.get("ID") in referenced:
            covered.add(_norm(fp).lower())
    return covered


def policy_covers_required_paths(doc, install_root: str | Path, *,
                                 dotnet_root: str | Path | None = None,
                                 extra_paths: list[str | Path] | None = None) -> bool:
    """True iff every required FilePath is present as an Allow rule **and** referenced
    in the UMCI signing scenario. This is the decision-3 self-protect validator."""
    root = _root(doc)
    covered = _covered_filepaths(root)
    required = required_filepaths(install_root, dotnet_root=dotnet_root, extra_paths=extra_paths)
    return all(_norm(r).lower() in covered for r in required)


def missing_required_paths(doc, install_root: str | Path, *,
                           dotnet_root: str | Path | None = None,
                           extra_paths: list[str | Path] | None = None) -> list[str]:
    """The required FilePaths NOT covered (empty list == fully covered). Lets the
    manifest validator report exactly what a rejected push is missing."""
    root = _root(doc)
    covered = _covered_filepaths(root)
    required = required_filepaths(install_root, dotnet_root=dotnet_root, extra_paths=extra_paths)
    return [r for r in required if _norm(r).lower() not in covered]
