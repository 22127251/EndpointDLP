"""
Examples
--------
  # Inline single file + folder
  python add-wdacwrule.py -i base.xml -o out.xml --allow-paths "C:\\Tools\\app.exe" "C:\\Tools\\good_apps" -c

  # List files
  python add-wdacwrule.py -i base.xml -o out.xml --allow-list allow.txt --deny-list deny.txt -c
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path


CIPOLICIES_ACTIVE = Path(r"C:\Windows\System32\CodeIntegrity\CIPolicies\Active")


def is_admin() -> bool:
    """True if the current process is elevated (member of BUILTIN\\Administrators)."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def collect_files(paths: list[str]) -> list[str]:
    """Expand a mixed list of file/directory paths into a flat list of file paths.

    Directories are walked recursively. Paths that do not exist raise
    FileNotFoundError. The returned paths are absolute.
    """
    resolved: list[str] = []
    for raw in paths:
        if not raw:
            continue
        p = Path(raw)
        if not p.exists():
            raise FileNotFoundError(f"Path not found: {raw}")
        if p.is_file():
            resolved.append(str(p.resolve()))
        elif p.is_dir():
            for child in p.rglob("*"):
                if child.is_file():
                    resolved.append(str(child.resolve()))
        else:
            raise FileNotFoundError(f"Not a file or directory: {raw}")
    return resolved


def read_list_file(path: str) -> list[str]:
    """Read a text file with one path per line. Blank lines and lines
    starting with '#' are ignored. Surrounding whitespace is stripped.
    A UTF-8 BOM at the start of the file is tolerated."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"List file not found: {path}")
    raw = p.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig")
    else:
        text = raw.decode("utf-8", errors="replace")
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Wrapper for ConfigCI with folder flattening."
    )
    p.add_argument("-i", "--input-policy", required=True,
                   help="Input SiPolicy XML path")
    p.add_argument("-o", "--output-policy",
                   default="out.xml",
                   help="Output SiPolicy XML path")
    p.add_argument("-a", "--allow-list",
                   help="Path to a text file listing Allow targets (one per line)")
    p.add_argument("-d", "--deny-list",
                   help="Path to a text file listing Deny targets (one per line)")
    p.add_argument("--allow-paths", nargs="+", metavar="PATH",
                   help="Inline Allow targets (files and/or directories)")
    p.add_argument("--deny-paths", nargs="+", metavar="PATH",
                   help="Inline Deny targets (files and/or directories)")
    p.add_argument("-f", "--file-name-level",
                   choices=["InternalName", "OriginalFileName",
                            "FileDescription", "ProductName"],
                   default="InternalName",
                   help="PE version-info field to use (default: InternalName)")
    p.add_argument("-c", "--compile-binary", action="store_true",
                   help="Also compile the output XML to a .cip binary")
    p.add_argument("--deploy", action="store_true",
                   help="After compile, copy the .cip to CIPolicies\\Active and "
                        "run 'citool -r' to refresh the running policy. "
                        "Implies -c. Requires an elevated shell.")
    p.add_argument("-s", "--script",
                   default=str(Path(__file__).with_name("Add-WDACRule.ps1")),
                   help="Path to Add-WDACRule.ps1 (default: alongside this script)")
    p.add_argument("--powershell", default="powershell",
                   help="PowerShell host (default: powershell). Use 'pwsh' for PS Core.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not Path(args.input_policy).is_file():
        print(f"error: input policy not found: {args.input_policy}",
              file=sys.stderr)
        return 2
    if not Path(args.script).is_file():
        print(f"error: PowerShell script not found: {args.script}",
              file=sys.stderr)
        return 2

    allow_raw: list[str] = []
    if args.allow_list:
        allow_raw.extend(read_list_file(args.allow_list))
    if args.allow_paths:
        allow_raw.extend(args.allow_paths)

    deny_raw: list[str] = []
    if args.deny_list:
        deny_raw.extend(read_list_file(args.deny_list))
    if args.deny_paths:
        deny_raw.extend(args.deny_paths)

    if not allow_raw and not deny_raw:
        print("error: at least one of --allow-list/--allow-paths or "
              "--deny-list/--deny-paths must be provided", file=sys.stderr)
        return 2

    try:
        allow_files = collect_files(allow_raw) if allow_raw else []
        deny_files = collect_files(deny_raw) if deny_raw else []
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not allow_files and not deny_files:
        print("error: no files collected from the provided paths", file=sys.stderr)
        return 2

    print(f"Allow targets : {len(allow_files)} file(s)")
    for f in allow_files:
        print(f"  + {f}")
    print(f"Deny targets  : {len(deny_files)} file(s)")
    for f in deny_files:
        print(f"  - {f}")

    if args.deploy and not args.compile_binary:
        args.compile_binary = True
        print("--deploy implies --compile-binary")

    cmd = [
        args.powershell,
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", args.script,
        "-InputPolicy", os.path.abspath(args.input_policy),
        "-OutputPolicy", os.path.abspath(args.output_policy),
        "-FileNameLevel", args.file_name_level,
    ]
    if allow_files:
        cmd.extend(["-AllowPaths", ",".join(allow_files)])
    if deny_files:
        cmd.extend(["-DenyPaths", ",".join(deny_files)])
    if args.compile_binary:
        cmd.append("-CompileBinary")

    print()
    print("Running:", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        return result.returncode

    if args.deploy:
        return deploy(args.output_policy)
    return 0


def deploy(output_xml: str) -> int:
    """Copy the .cip produced next to output_xml into CIPolicies\\Active
    and run 'citool -r' to refresh the running policy."""
    if not is_admin():
        print("error: --deploy requires an elevated (admin) shell. "
              "Re-run from an Administrator PowerShell/cmd prompt.",
              file=sys.stderr)
        return 5

    cip_path = find_compiled_cip(output_xml)
    if cip_path is None:
        print("error: could not locate the compiled .cip next to "
              f"{output_xml}. The compile step may have failed silently.",
              file=sys.stderr)
        return 6

    CIPOLICIES_ACTIVE.mkdir(parents=True, exist_ok=True)
    dest = CIPOLICIES_ACTIVE / cip_path.name
    print()
    print(f"Deploying: {cip_path}")
    print(f"     ->    {dest}")
    try:
        shutil.copy2(cip_path, dest)
    except OSError as e:
        print(f"error: failed to copy to Active folder: {e}",
              file=sys.stderr)
        return 7

    print()
    print("Refreshing CI policy: echo . | citool -r")
    cp = subprocess.run("echo . | citool -r", shell=True)
    if cp.returncode != 0:
        print(f"warning: citool -r returned {cp.returncode}",
              file=sys.stderr)
        return cp.returncode
    print("Deployed and refreshed.")
    return 0


def find_compiled_cip(output_xml: str) -> Path | None:
    """Locate the .cip that Add-WDACRule.ps1 produced. It is
    written next to output_xml as '{PolicyID}.cip'."""
    out_dir = Path(os.path.abspath(output_xml)).parent
    if not out_dir.is_dir():
        return None
    import re
    xml_text = Path(output_xml).read_text(encoding="utf-8", errors="replace")
    m = re.search(r"<PolicyID>\s*(\{[0-9A-Fa-f-]+\})\s*</PolicyID>", xml_text)
    if m:
        cand = out_dir / f"{m.group(1)}.cip"
        if cand.is_file():
            return cand
    cips = sorted(out_dir.glob("*.cip"))
    return cips[0] if cips else None


if __name__ == "__main__":
    sys.exit(main())
