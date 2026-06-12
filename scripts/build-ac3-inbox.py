"""Build the AC-3 VM end-to-end inbox pushes (a good one + a deliberately-bad one).

Produces, under ``--out`` (default ``tmp\\ac3``):
  - ``good\\``  {policy.xml, {PolicyID}.cip, manifest.json} — a valid push that denies
               ``olk.exe`` (new Outlook) + ``OneDrive.exe`` and carries the agent's
               self-protect FilePath rules. Passes ``manifest.validate_all``.
  - ``bad\\``   the same files but with a corrupted ``.cip`` hash in the manifest —
               the rejection-path push (validate_all returns a hash_mismatch).

Copy ``good\\`` (and later ``bad\\``) into the VM's
``%ProgramData%\\DLP\\appcontrol\\inbox\\`` as a subfolder; the running DLPAgent
service's inbox watcher does the rest. (olk.exe is Store-signed, so the explicit
Deny proves deny-beats-base-allow; OneDrive's signing chain fails the base allow
set anyway — both surface as 3077 carrying our PolicyGUID, which the forwarder logs.)

Run on the dev box (ConfigCI present per AC-1), from the repo root, .venv active:
    python scripts\\build-ac3-inbox.py
Override paths if the VM differs from the defaults:
    python scripts\\build-ac3-inbox.py --install-root "C:\\Program Files\\DLP" ^
        --dotnet-root "C:\\Program Files\\dotnet" --version 10.7.0.1
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.app_control import manifest as mf  # noqa: E402
from orchestrator.app_control import policy_xml as px  # noqa: E402
from orchestrator.app_control import selfprotect as sp  # noqa: E402


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


def _write_manifest(folder: Path, policy_id: str, version: str, *, corrupt_cip: bool) -> None:
    pxml = folder / "policy.xml"
    cip = folder / f"{policy_id}.cip"
    man = {
        "schema_version": 1,
        "policy_id": policy_id,
        "version_ex": version,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "build-ac3-inbox",
        "files": {
            "policy_xml": {"name": "policy.xml", "sha256": mf.flat_sha256(pxml)},
            "cip": {"name": cip.name,
                    "sha256": ("0" * 64) if corrupt_cip else mf.flat_sha256(cip)},
        },
    }
    (folder / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")


def _build_policy(install_root: str, dotnet_root: str, version: str):
    doc = px.load_base_policy()
    px.set_version_ex(doc, version)
    px.set_policy_info_id(doc, "AC3-vm-test")
    # olk.exe (new Outlook) is Store-signed → base allows it → explicit Deny proves
    # deny-beats-allow. OneDrive.exe is denied too (its chain also fails base allow).
    px.add_file_attrib_rule(doc, "InternalName", "olk", allow=False)
    px.add_file_attrib_rule(doc, "OriginalFileName", "OneDrive.exe", allow=False)
    sp.add_selfprotect_rules(doc, install_root, dotnet_root=dotnet_root)
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the AC-3 VM inbox pushes.")
    ap.add_argument("--out", default=str(_REPO_ROOT / "tmp" / "ac3"))
    ap.add_argument("--install-root", default=r"C:\Program Files\DLP")
    ap.add_argument("--dotnet-root", default=r"C:\Program Files\dotnet")
    ap.add_argument("--version", default="10.7.0.1",
                    help="VersionEx for the push (must exceed any deployed policy)")
    ap.add_argument("--powershell", default="powershell")
    args = ap.parse_args(argv)

    out = Path(args.out)
    policy_id = px.get_policy_id(px.load_base_policy())
    print(f"policy_id   = {policy_id}")
    print(f"version_ex  = {args.version}")
    print(f"install_root= {args.install_root}")
    print(f"dotnet_root = {args.dotnet_root}")
    print(f"output      = {out}\n")

    doc = _build_policy(args.install_root, args.dotnet_root, args.version)

    good = out / "good"
    good.mkdir(parents=True, exist_ok=True)
    px.serialize(doc, good / "policy.xml")
    _compile(good / "policy.xml", good / f"{policy_id}.cip", args.powershell)
    _write_manifest(good, policy_id, args.version, corrupt_cip=False)
    print(f"  OK  good/  ({(good / f'{policy_id}.cip').stat().st_size} byte .cip)")

    # bad/ reuses the same compiled policy but corrupts the manifest's cip hash.
    bad = out / "bad"
    bad.mkdir(parents=True, exist_ok=True)
    px.serialize(doc, bad / "policy.xml")
    (bad / f"{policy_id}.cip").write_bytes((good / f"{policy_id}.cip").read_bytes())
    _write_manifest(bad, policy_id, args.version, corrupt_cip=True)
    print("  OK  bad/   (manifest cip sha256 corrupted)")

    # --- dev self-check: validate the pushes exactly as the runtime watcher will ---
    good_fail = mf.validate_all(
        mf.parse_manifest((good / "manifest.json").read_text(encoding="utf-8")), good,
        deployed_version_ex=None, install_root=args.install_root, dotnet_root=args.dotnet_root)
    bad_fail = mf.validate_all(
        mf.parse_manifest((bad / "manifest.json").read_text(encoding="utf-8")), bad,
        deployed_version_ex=None, install_root=args.install_root, dotnet_root=args.dotnet_root)
    print(f"\nself-check: validate_all(good) -> {good_fail}")
    print(f"self-check: validate_all(bad)  -> {[f.code for f in bad_fail]}")
    if good_fail:
        raise SystemExit("FAIL: good push did not validate clean")
    if not bad_fail:
        raise SystemExit("FAIL: bad push unexpectedly validated clean")

    print(f"\nALL OK. Copy '{good}' into the VM's "
          r"%ProgramData%\DLP\appcontrol\inbox\ as a subfolder for S1;"
          f"\nuse '{bad}' for the rejection test (S5).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
