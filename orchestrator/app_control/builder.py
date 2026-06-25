"""Operator-side WDAC policy authoring for ``dlp-ctl appcontrol`` (Phase AC-4).

The standalone build pipeline the elevated admin drives locally:

  - ``add_entries`` / ``remove_entries`` — maintain the allow/deny **lists**
    (raw file + folder paths, one per line).
  - ``build()``   — run the AC-2 engine over the lists (FileAttrib on InternalName,
    auto Hash-fallback for files with no PE version-info) + the mandatory self-protect
    rules, bump VersionEx above the deployed policy, compile via ``ConvertFrom-CIPolicy``,
    and write a ``{policy.xml, {GUID}.cip, manifest.json}`` push into ``staging\\build\\``.
  - ``apply()``   — atomically move the staged push into the inbox (the go-live gate);
    the AC-3 inbox watcher deploys it.
  - ``disable_local()`` — the ``--force-local`` emergency removal: drive the AC-3
    deployer's ``remove()`` in-process, with no dependency on a healthy agent.

``build`` / ``apply`` / list management are **offline** (no running agent needed).
``dlp-ctl appcontrol status`` / ``disable`` (without ``--force-local``) instead go
through the admin-pipe. This module is import-light (no analyzer deps), so
``ctl.py`` can lazy-import it.

Reuses the AC-2/AC-3 building blocks unchanged: ``policy_xml`` (rule insertion),
``selfprotect`` (the mandatory FilePath self-coverage + the dotnet carry-forward),
``hashing`` (the no-PE-metadata fallback), ``manifest`` (schema + ``validate_all``),
``deployer`` (``citool`` remove). Build-step shape mirrors ``scripts/build-ac3-inbox.py``.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import hashing
from . import manifest as mf
from . import paths
from . import policy_xml as px
from . import selfprotect as sp
from .deployer import Deployer

log = logging.getLogger("orchestrator.app_control.builder")

#: Executable extensions a WDAC FileAttrib/Hash rule can meaningfully cover (PE files).
_EXE_EXTS = {".exe", ".dll", ".sys", ".ocx", ".scr", ".cpl", ".efi"}

#: The single staged-push subfolder under ``staging\`` (one un-applied build at a time).
_BUILD_SUBDIR = "build"


class BuildError(RuntimeError):
    """A build/apply failed (empty lists, bad path, compile error, or the produced
    push failed self-validation)."""


# --------------------------------------------------------------------------- #
# allow/deny list files
# --------------------------------------------------------------------------- #

def read_entries(list_path: str | Path) -> list[str]:
    """Entries (raw file/folder paths) from a list file: one per line; blank lines
    and ``#`` comments skipped; UTF-8 BOM tolerated; missing file -> ``[]``."""
    p = Path(list_path)
    if not p.is_file():
        return []
    raw = p.read_bytes()
    text = (raw.decode("utf-8-sig") if raw.startswith(b"\xef\xbb\xbf")
            else raw.decode("utf-8", errors="replace"))
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _write_entries(list_path: str | Path, entries: list[str]) -> None:
    """Atomically (temp + ``os.replace``) write entries, one per line."""
    p = Path(list_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("".join(e + "\n" for e in entries), encoding="utf-8")
    os.replace(tmp, p)


def _norm_entry(raw) -> str:
    return str(raw).strip().strip('"')


def add_entries(list_path: str | Path, new_paths) -> tuple[list[str], list[str]]:
    """Add ``new_paths`` to the list (case-insensitive dedup, order preserved).
    Returns ``(added, all_entries)``. Paths are stored **raw** — folders are
    re-scanned at build time, so a folder picks up newly-installed files."""
    entries = read_entries(list_path)
    have = {e.lower() for e in entries}
    added: list[str] = []
    for raw in new_paths:
        s = _norm_entry(raw)
        if s and s.lower() not in have:
            have.add(s.lower())
            entries.append(s)
            added.append(s)
    _write_entries(list_path, entries)
    return added, entries


def remove_entries(list_path: str | Path, paths_to_remove) -> tuple[list[str], list[str]]:
    """Remove matching entries (case-insensitive). Returns ``(removed, remaining)``."""
    entries = read_entries(list_path)
    drop = {_norm_entry(p).lower() for p in paths_to_remove}
    removed = [e for e in entries if e.lower() in drop]
    remaining = [e for e in entries if e.lower() not in drop]
    _write_entries(list_path, remaining)
    return removed, remaining


def collect_files(entries) -> list[str]:
    """Expand raw file/folder entries into a flat, de-duplicated list of absolute
    **executable** file paths. Folders are walked recursively (``rglob``); non-PE
    files are skipped. A missing path raises ``FileNotFoundError`` — the operator
    gave a bad path (ported from ``add-wdacwrule.py``'s ``collect_files``, plus the
    extension filter so WDAC rules only target PE files)."""
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in entries:
        s = _norm_entry(raw)
        if not s:
            continue
        p = Path(s)
        if not p.exists():
            raise FileNotFoundError(f"path not found: {s}")
        candidates = [p] if p.is_file() else [c for c in p.rglob("*") if c.is_file()]
        for c in candidates:
            if c.suffix.lower() not in _EXE_EXTS:
                continue
            ap = str(c.resolve())
            if ap.lower() not in seen:
                seen.add(ap.lower())
                resolved.append(ap)
    return resolved


# --------------------------------------------------------------------------- #
# version helpers
# --------------------------------------------------------------------------- #

def _ver_tuple(v: str) -> tuple[int, int, int, int] | None:
    parts = str(v).strip().split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def _max_version(*versions: str | None) -> str:
    best_t: tuple[int, int, int, int] | None = None
    for v in versions:
        if not v:
            continue
        t = _ver_tuple(v)
        if t is not None and (best_t is None or t > best_t):
            best_t = t
    return ".".join(str(x) for x in (best_t or (0, 0, 0, 0)))


def _bump(version: str) -> str:
    t = _ver_tuple(version) or (0, 0, 0, 0)
    return f"{t[0]}.{t[1]}.{t[2]}.{t[3] + 1}"


def _validate_explicit_version(version: str, deployed: str | None) -> None:
    t = _ver_tuple(version)
    if t is None:
        raise BuildError(f"--version must be 4 dotted integers, got {version!r}")
    if deployed:
        dt = _ver_tuple(deployed)
        if dt is not None and t <= dt:
            raise BuildError(
                f"--version {version} must exceed the currently deployed {deployed}")


def _read_staged_version(build_dir: Path) -> str | None:
    mpath = build_dir / "manifest.json"
    if not mpath.is_file():
        return None
    try:
        return mf.parse_manifest(mpath.read_text(encoding="utf-8")).version_ex
    except (mf.ManifestError, OSError):
        return None


# --------------------------------------------------------------------------- #
# compile + manifest
# --------------------------------------------------------------------------- #

def _default_compiler(xml_path: Path, cip_path: Path, *, powershell: str = "powershell") -> None:
    """Compile a SiPolicy XML to a ``.cip`` via ``ConvertFrom-CIPolicy`` (Windows
    PowerShell 5.1 — the ConfigCI host; **not** ``pwsh``). Preflights ConfigCI and
    raises the DISM-enable hint if it is absent (reusing ``hashing``'s preflight)."""
    hashing._preflight_configci(powershell)
    ps = ("$ErrorActionPreference='Stop'; "
          f"ConvertFrom-CIPolicy -XmlFilePath '{xml_path}' -BinaryFilePath '{cip_path}' | Out-Null")
    proc = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", ps],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not Path(cip_path).is_file():
        raise BuildError(
            f"ConvertFrom-CIPolicy failed (rc={proc.returncode}) for {xml_path.name}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}")


def _write_manifest(folder: Path, policy_id: str, version: str) -> None:
    xml = folder / "policy.xml"
    cip = folder / f"{policy_id}.cip"
    man = {
        "schema_version": mf.SUPPORTED_SCHEMA_VERSION,
        "policy_id": policy_id,
        "version_ex": version,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "dlp-ctl",
        "files": {
            "policy_xml": {"name": "policy.xml", "sha256": mf.flat_sha256(xml)},
            "cip": {"name": cip.name, "sha256": mf.flat_sha256(cip)},
        },
    }
    (folder / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# build / apply / disable
# --------------------------------------------------------------------------- #

def build(config, *, version: str | None = None, compiler=None,
          powershell: str = "powershell") -> dict:
    """Build + compile a push from the allow/deny lists into ``staging\\build\\``.

    Always merges the self-protect FilePath rules (``<install_root>\\*`` +
    ``C:\\Program Files\\dotnet\\*`` — the AC-2 carry-forward). VersionEx is bumped
    above the deployed policy (and any already-staged build); ``version`` overrides.
    The produced push is self-validated with ``manifest.validate_all`` and a failure
    raises ``BuildError`` — so ``apply`` can only ship a push the watcher will accept.

    ``compiler(xml_path, cip_path)`` is the injectable compile seam (default real
    ``ConvertFrom-CIPolicy``); tests pass a stub.
    """
    do_compile = compiler or (lambda x, c: _default_compiler(x, c, powershell=powershell))

    install_root = paths.install_root(config)
    dotnet_root = paths.dotnet_root(config)
    extra = paths.extra_paths(config)
    build_dir = paths.staging_dir(config) / _BUILD_SUBDIR

    allow_files = collect_files(read_entries(paths.allow_list_path(config)))
    deny_files = collect_files(read_entries(paths.deny_list_path(config)))
    if not allow_files and not deny_files:
        raise BuildError(
            "allow-list and deny-list are empty (no executables collected); add "
            "targets with `dlp-ctl appcontrol allow|deny <path>` first.")

    base_doc = px.load_base_policy()
    policy_id = px.get_policy_id(base_doc)
    doc = px.load_base_policy()

    hashed: list[str] = []
    warnings: list[str] = []
    for files, allow in ((allow_files, True), (deny_files, False)):
        for f in files:
            attr = px.read_file_attribute(f, "InternalName")
            if attr:
                px.add_file_attrib_rule(doc, "InternalName", attr, allow=allow)
                warnings.extend(px.warn_on_risky_attribute("InternalName", attr))
            else:
                # No usable PE version-info -> WDAC Hash fallback (decision 5).
                hashes = hashing.compute_wdac_hashes(f, powershell=powershell)
                px.add_hash_rules(doc, Path(f).name, hashes, allow=allow)
                hashed.append(f)

    sp.add_selfprotect_rules(doc, install_root, dotnet_root=dotnet_root, extra_paths=extra)

    deployer = Deployer(status_path=paths.status_path(config), policy_id=policy_id)
    deployed = deployer.deployed_version_ex()
    if version:
        _validate_explicit_version(version, deployed)
        new_version = version
    else:
        floor = _max_version(deployed, px.get_version_ex(base_doc),
                             _read_staged_version(build_dir))
        new_version = _bump(floor)
    px.set_version_ex(doc, new_version)
    px.set_policy_info_id(doc, new_version)

    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    xml_path = build_dir / "policy.xml"
    cip_path = build_dir / f"{policy_id}.cip"
    px.serialize(doc, xml_path)
    do_compile(xml_path, cip_path)
    if not cip_path.is_file():
        raise BuildError("compile step produced no .cip")
    _write_manifest(build_dir, policy_id, new_version)

    # Self-validate exactly as the AC-3 watcher will (fail-closed, decision D5).
    m = mf.parse_manifest((build_dir / "manifest.json").read_text(encoding="utf-8"))
    failures = mf.validate_all(m, build_dir, deployed_version_ex=deployed,
                               install_root=install_root, dotnet_root=dotnet_root,
                               extra_paths=extra)
    if failures:
        raise BuildError("built policy failed self-validation: "
                         + "; ".join(f"{f.code}: {f.detail}" for f in failures))

    log.info("Built policy %s v%s (%d allow, %d deny, %d hashed) -> %s",
             policy_id, new_version, len(allow_files), len(deny_files),
             len(hashed), build_dir)
    return {
        "policy_id": policy_id,
        "version_ex": new_version,
        "deployed_version_ex": deployed,
        "allow_files": len(allow_files),
        "deny_files": len(deny_files),
        "hashed": hashed,
        "warnings": warnings,
        "staging_dir": str(build_dir),
    }


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def apply(config) -> dict:
    """Move the staged push into the inbox (the explicit go-live gate). The whole
    subfolder appears atomically (``os.replace`` rename, same volume), so the AC-3
    watcher's manifest-last + size-stable pickup is satisfied trivially."""
    build_dir = paths.staging_dir(config) / _BUILD_SUBDIR
    if not (build_dir / "manifest.json").is_file():
        raise BuildError("no staged build to apply; run `dlp-ctl appcontrol build` first.")
    inbox = paths.inbox_dir(config)
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / f"{_utc_stamp()}_build"
    n = 1
    while dest.exists():
        dest = inbox / f"{_utc_stamp()}_build_{n}"
        n += 1
    try:
        os.replace(build_dir, dest)          # atomic rename (same volume)
    except OSError:
        shutil.move(str(build_dir), str(dest))  # cross-volume fallback (watcher tolerates)
    log.info("Applied staged push -> %s", dest)
    return {"applied": str(dest)}


def disable_local(config) -> dict:
    """``--force-local`` emergency disable: remove our deployed policy in-process via
    the AC-3 deployer (``citool --remove-policy`` + neutralizer fallback). Needs no
    running agent — the escape hatch for when the service is dead."""
    policy_id = px.get_policy_id(px.load_base_policy())
    deployer = Deployer(status_path=paths.status_path(config), policy_id=policy_id)
    removed = deployer.remove()
    st = deployer.read_status()
    return {"removed": removed, "policy_guid": st.get("policy_guid"),
            "last_error": st.get("last_error")}
