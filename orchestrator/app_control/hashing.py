"""WDAC hash-rule values via ``New-CIPolicyRule``/``New-CIPolicy`` (the one OS-touching seam).

A WDAC Hash rule is not a single SHA-256: App Control precalculates four values per
file (SHA1/SHA256 Authenticode + SHA1/SHA256 page hash), and the page hashes need
PE-layout-aware hashing. Rather than reimplement that in Python (high risk — a wrong
self-protect/allow hash silently blocks a binary), we shell out to Microsoft's own
ConfigCI cmdlets and lift the ``<Allow Hash="...">`` values out of the policy they
produce. This is the **rare** fallback for operator-chosen files with no usable PE
version info (decision 5); self-protect never needs it (it uses FilePath rules).

The shell-out is isolated behind an injectable ``runner`` so the XML-extraction logic
stays pure and unit-testable without PowerShell.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from . import SIPOLICY_NS

log = logging.getLogger(__name__)

_DISM_HINT = (
    r"Enable it offline (admin): "
    r"gci $Env:SystemRoot\servicing\Packages\*ConfigCI*.mum | "
    r'% { dism /online /norestart /add-package:"$($_.FullName)" }'
)


def _extract_hashes(policy_xml_text: str) -> list[str]:
    """Pull the distinct ``Hash`` values out of the ``<Allow Hash=...>`` rules of a
    New-CIPolicy-produced policy XML, order-preserving. Pure (no I/O)."""
    root = ET.fromstring(policy_xml_text)
    hashes: list[str] = []
    seen: set[str] = set()
    for allow in root.iter(f"{{{SIPOLICY_NS}}}Allow"):
        h = allow.get("Hash")
        if h and h not in seen:
            seen.add(h)
            hashes.append(h)
    return hashes


def _preflight_configci(powershell: str) -> None:
    """Raise a clear, actionable error if the ConfigCI module is not present."""
    probe = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command",
         "if (Get-Command New-CIPolicy -ErrorAction SilentlyContinue) { exit 0 } else { exit 9 }"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            "ConfigCI module not available (New-CIPolicy missing). " + _DISM_HINT
        )


def _powershell_runner(file_path: Path, *, powershell: str = "powershell") -> str:
    """Default runner: copy ``file_path`` into an isolated scan dir, run
    ``New-CIPolicy -Level Hash`` over just that dir, and return the policy XML text.
    The Authenticode/page hashes are content-based, so hashing the copy matches the
    original (the decision-8a property)."""
    file_path = Path(file_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"file to hash not found: {file_path}")
    _preflight_configci(powershell)

    workdir = Path(tempfile.mkdtemp(prefix="dlp-wdac-hash-"))
    try:
        scan_dir = workdir / "scan"
        scan_dir.mkdir()
        shutil.copy2(file_path, scan_dir / file_path.name)
        out_xml = workdir / "hashpolicy.xml"
        ps = (
            "$ErrorActionPreference='Stop'; "
            f"New-CIPolicy -Level Hash -UserPEs -NoScript "
            f"-ScanPath '{scan_dir}' -FilePath '{out_xml}' | Out-Null"
        )
        proc = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", ps],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not out_xml.is_file():
            raise RuntimeError(
                f"New-CIPolicy -Level Hash failed (rc={proc.returncode}) for "
                f"{file_path}: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return out_xml.read_text(encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def compute_wdac_hashes(
    file_path: str | Path,
    *,
    runner: Callable[[Path], str] | None = None,
    powershell: str = "powershell",
) -> list[str]:
    """Return the WDAC hash values for ``file_path`` (typically four). ``runner`` is
    injectable for tests; by default it shells out to ``New-CIPolicy`` on the
    endpoint. Raises if the file is missing, ConfigCI is absent, or no hash is found."""
    run = runner or (lambda p: _powershell_runner(p, powershell=powershell))
    policy_xml_text = run(Path(file_path))
    hashes = _extract_hashes(policy_xml_text)
    if not hashes:
        raise RuntimeError(f"no <Allow Hash> rules produced for {file_path}")
    log.info("computed %d WDAC hash value(s) for %s", len(hashes), file_path)
    return hashes
