"""Build (and compile) the AC-2 verification policies.

Produces, under ``--out`` (default ``tmp\\ac2``):
  - ``v6\\``  a *representative* policy (FileAttrib allow+deny + self-protect FilePath
              for install_root + dotnet + real Hash rules) — the AC2-T10 / V6 dev
              compile cross-check. Side-effect-free: it compiles with
              ``ConvertFrom-CIPolicy`` but is NEVER deployed.
  - ``B\\``   Policy B for the VM self-protect test: base + ``<install_root>\\*`` only
              (VersionEx 10.4.0.1).
  - ``C\\``   Policy C for the VM self-protect test: base + ``<install_root>\\*`` +
              ``<dotnet_root>\\*`` (VersionEx 10.4.0.2).

Each ``.cip`` is named ``{PolicyID}.cip`` (shared PolicyID), so B and C land in
separate folders. Copy ``B`` and ``C`` to the VM (e.g. ``C:\\ac2-test\\B`` / ``...\\C``)
for the procedure in the AC-2 plan.

Run on the dev box (ConfigCI present), from the repo root, .venv active:
    python scripts\\build-ac2-policies.py
Override the paths if the VM differs:
    python scripts\\build-ac2-policies.py --install-root "C:\\Program Files\\DLP" ^
        --dotnet-root "C:\\Program Files\\dotnet"
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.app_control import hashing  # noqa: E402
from orchestrator.app_control import policy_xml as px  # noqa: E402
from orchestrator.app_control import selfprotect as sp  # noqa: E402


def _wildcard(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\") + r"\*"


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


def _emit(doc, out_dir: Path, powershell: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    guid = px.get_policy_id(doc)
    xml_path = out_dir / "policy.xml"
    cip_path = out_dir / f"{guid}.cip"
    px.serialize(doc, xml_path)
    _compile(xml_path, cip_path, powershell)
    print(f"  OK  {out_dir.name}: {xml_path.name} + {cip_path.name} "
          f"(VersionEx {px.get_version_ex(doc)})")
    return cip_path


def build_v6(out_dir: Path, install_root: str, dotnet_root: str, hash_sample: str,
             powershell: str) -> None:
    doc = px.load_base_policy()
    px.set_version_ex(doc, "10.9.0.1")
    px.set_policy_info_id(doc, "AC2-V6-representative")
    px.add_file_attrib_rule(doc, "InternalName", "7zFM", allow=True)
    px.add_file_attrib_rule(doc, "InternalName", "olk", allow=False)
    sp.add_selfprotect_rules(doc, install_root, dotnet_root=dotnet_root)
    hashes = hashing.compute_wdac_hashes(hash_sample, powershell=powershell)
    px.add_hash_rules(doc, Path(hash_sample).name, hashes)
    _emit(doc, out_dir, powershell)


def build_policy(out_dir: Path, version: str, install_root: str,
                 dotnet_root: str | None, powershell: str) -> None:
    doc = px.load_base_policy()
    px.set_version_ex(doc, version)
    px.set_policy_info_id(doc, f"AC2-selfprotect-{out_dir.name}")
    px.add_filepath_rule(doc, _wildcard(install_root), allow=True)
    if dotnet_root is not None:
        px.add_filepath_rule(doc, _wildcard(dotnet_root), allow=True)
    _emit(doc, out_dir, powershell)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the AC-2 verification policies.")
    ap.add_argument("--out", default=str(_REPO_ROOT / "tmp" / "ac2"))
    ap.add_argument("--install-root", default=r"C:\Program Files\DLP")
    ap.add_argument("--dotnet-root", default=r"C:\Program Files\dotnet")
    ap.add_argument("--hash-sample", default=r"C:\Windows\System32\notepad.exe",
                    help="real PE used to exercise the Hash-rule path in the V6 policy")
    ap.add_argument("--powershell", default="powershell")
    args = ap.parse_args(argv)

    out = Path(args.out)
    print(f"install_root = {args.install_root}")
    print(f"dotnet_root  = {args.dotnet_root}")
    print(f"output       = {out}")
    print("building + compiling (ConvertFrom-CIPolicy, no deploy):")

    build_v6(out / "v6", args.install_root, args.dotnet_root, args.hash_sample, args.powershell)
    build_policy(out / "B", "10.4.0.1", args.install_root, None, args.powershell)
    build_policy(out / "C", "10.4.0.2", args.install_root, args.dotnet_root, args.powershell)

    print("\nALL COMPILED OK.")
    print(f"VM Policy B: {out / 'B'}   (copy to C:\\ac2-test\\B)")
    print(f"VM Policy C: {out / 'C'}   (copy to C:\\ac2-test\\C)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
