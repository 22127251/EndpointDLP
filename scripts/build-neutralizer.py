"""Build + compile the committed App Control neutralizer policy (AC-3).

Produces, as package data under ``orchestrator/app_control/``:
  - ``neutralizer.xml`` — AllowAll restamped with our PolicyID + maximal VersionEx.
  - ``neutralizer.cip`` — the compiled binary the deployer ships and deploys as the
    emergency-disable fallback (parent decision 4).

The PolicyID is read from the packaged ``base.xml`` (single source of truth), so
the neutralizer always shares our enforcement policy's identity.

Run on the dev box (ConfigCI present per AC-1), from the repo root, .venv active:
    python scripts\\build-neutralizer.py

Side-effect-free w.r.t. the running system: it compiles with ``ConvertFrom-CIPolicy``
but never deploys. Commit both outputs.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.app_control import neutralizer as nz  # noqa: E402
from orchestrator.app_control import policy_xml as px  # noqa: E402


def _compile(xml_path: Path, cip_path: Path, powershell: str) -> None:
    ps = (
        "$ErrorActionPreference='Stop'; "
        f"ConvertFrom-CIPolicy -XmlFilePath '{xml_path}' -BinaryFilePath '{cip_path}' | Out-Null"
    )
    proc = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not cip_path.is_file():
        raise SystemExit(
            f"COMPILE FAILED for {xml_path.name} (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Build + compile the AC-3 neutralizer policy.")
    ap.add_argument("--powershell", default="powershell")
    args = ap.parse_args(argv)

    policy_id = px.get_policy_id(px.load_base_policy())
    print(f"policy_id  = {policy_id}")
    print(f"version_ex = {nz.NEUTRALIZER_VERSION_EX}")

    doc = nz.build_neutralizer_doc(policy_id)
    px.serialize(doc, nz.NEUTRALIZER_XML_PATH)
    print(f"wrote      {nz.NEUTRALIZER_XML_PATH}")

    _compile(nz.NEUTRALIZER_XML_PATH, nz.NEUTRALIZER_CIP_PATH, args.powershell)
    print(f"compiled   {nz.NEUTRALIZER_CIP_PATH} ({nz.NEUTRALIZER_CIP_PATH.stat().st_size} bytes)")
    print("OK — commit neutralizer.xml + neutralizer.cip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
