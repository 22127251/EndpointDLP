"""AllowAll "neutralizer" policy — the emergency-disable fallback (parent decision 4).

When ``citool --remove-policy`` cannot remove our deployed enforcement policy (a
wedged refresh, or a pre-24H2 build that needs a reboot to remove), the deployer
instead deploys this AllowAll policy. It shares our **PolicyID** with a maximal
``VersionEx``, so a single ``citool --refresh`` replaces the enforcement policy
with allow-everything *in place* (immediate relief); deleting the active ``.cip``
afterwards removes the policy entirely at the next boot. This is exactly the
mechanism rehearsed end-to-end on the VM in AC-1.

To avoid a runtime ConfigCI/compile dependency, ``neutralizer.cip`` is
**pre-compiled on dev** by ``scripts/build-neutralizer.py`` and committed as
package data (the installer copies ``orchestrator/`` wholesale, so it ships for
free, exactly like ``base.xml``). The runtime only needs
:func:`neutralizer_cip_path`.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

from . import SIPOLICY_NS
from . import policy_xml as px

_HERE = Path(__file__).resolve().parent
NEUTRALIZER_XML_PATH = _HERE / "neutralizer.xml"
NEUTRALIZER_CIP_PATH = _HERE / "neutralizer.cip"

#: Maximal 4×16-bit VersionEx so the neutralizer always out-versions any deployed
#: enforcement policy — WDAC refuses a refresh whose VersionEx <= the loaded one.
#: Nothing can be higher, so the override always wins.
NEUTRALIZER_VERSION_EX = "65535.65535.65535.65535"


def _default_template_path() -> Path:
    """The Windows-shipped AllowAll example policy (present on dev + the VM)."""
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    return Path(windir) / "schemas" / "CodeIntegrity" / "ExamplePolicies" / "AllowAll.xml"


def _q(tag: str) -> str:
    return f"{{{SIPOLICY_NS}}}{tag}"


def _set_text(root: ET.Element, tag: str, value: str) -> None:
    el = root.find(_q(tag))
    if el is None:
        raise ValueError(f"<{tag}> not found in AllowAll template")
    el.text = value


def build_neutralizer_doc(policy_id: str, *, template_path: str | Path | None = None
                          ) -> ET.ElementTree:
    """Load the Windows AllowAll template, restamp ``PolicyID``/``BasePolicyID`` to
    ``policy_id`` and ``VersionEx`` to the maximal value, and return the tree.

    Pure XML manipulation — no OS side effects (the compile happens in the keeper
    script). ``policy_id`` must be the braced GUID our enforcement policy uses so
    the neutralizer overwrites it in ``CIPolicies\\Active\\``.
    """
    template = Path(template_path) if template_path else _default_template_path()
    doc = ET.parse(template)
    root = doc.getroot()
    _set_text(root, "PolicyID", policy_id)
    _set_text(root, "BasePolicyID", policy_id)
    _set_text(root, "VersionEx", NEUTRALIZER_VERSION_EX)
    return doc


def neutralizer_cip_path() -> Path:
    """Path to the committed, pre-compiled neutralizer ``.cip`` (package data)."""
    return NEUTRALIZER_CIP_PATH
