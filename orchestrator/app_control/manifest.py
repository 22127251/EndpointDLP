"""Inbox manifest schema + validator suite (pure functions).

A push that lands in ``%ProgramData%\\DLP\\appcontrol\\inbox\\`` is ``{policy.xml,
{PolicyID}.cip, manifest.json}``. The manifest carries integrity hashes + identity so
the channel can validate a push before deploying it. These validators are shared by
the in-orchestrator runtime (AC-3) and the unit tests; the runtime moves a failing
push to ``rejected/`` and emits an event.

Note: ``sha256`` here is the **flat** SHA-256 of the file bytes (transport integrity),
which is a different thing from a WDAC Hash rule (an Authenticode/PE image hash).
"""
from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from . import selfprotect

SUPPORTED_SCHEMA_VERSION = 1


class ManifestError(ValueError):
    """Raised when a manifest is malformed (wrong type / missing field / bad JSON)."""


@dataclass(frozen=True)
class FileEntry:
    name: str
    sha256: str


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    policy_id: str
    version_ex: str
    created: str
    source: str
    policy_xml: FileEntry
    cip: FileEntry


@dataclass(frozen=True)
class Failure:
    code: str
    detail: str


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _file_entry(d: dict) -> FileEntry:
    return FileEntry(name=str(d["name"]), sha256=str(d["sha256"]))


def parse_manifest(data) -> Manifest:
    """Parse a manifest from JSON text/bytes or a dict. Strict — raises
    ``ManifestError`` on bad JSON or any missing/invalid field."""
    if isinstance(data, (str, bytes, bytearray)):
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, ValueError) as e:
            raise ManifestError(f"invalid manifest JSON: {e}") from e
    elif isinstance(data, dict):
        obj = data
    else:
        raise ManifestError(f"manifest must be JSON text or dict, got {type(data).__name__}")

    if not isinstance(obj, dict):
        raise ManifestError("manifest root must be an object")
    try:
        files = obj["files"]
        return Manifest(
            schema_version=int(obj["schema_version"]),
            policy_id=str(obj["policy_id"]),
            version_ex=str(obj["version_ex"]),
            created=str(obj["created"]),
            source=str(obj["source"]),
            policy_xml=_file_entry(files["policy_xml"]),
            cip=_file_entry(files["cip"]),
        )
    except (KeyError, TypeError) as e:
        raise ManifestError(f"malformed manifest: missing/invalid field {e}") from e


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def flat_sha256(path: str | Path) -> str:
    """Plain SHA-256 of a file's bytes (transport integrity — NOT a WDAC hash)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ver_tuple(v: str) -> tuple[int, int, int, int]:
    parts = str(v).strip().split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        raise ValueError(f"VersionEx must be 4 dotted integers, got {v!r}")
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Individual validators (each returns a Failure or None)
# --------------------------------------------------------------------------- #

def validate_schema_version(m: Manifest) -> Failure | None:
    if m.schema_version != SUPPORTED_SCHEMA_VERSION:
        return Failure("schema_version",
                       f"unsupported schema_version {m.schema_version} "
                       f"(expected {SUPPORTED_SCHEMA_VERSION})")
    return None


def validate_cip_name_matches_policy_id(m: Manifest) -> Failure | None:
    expected = f"{m.policy_id}.cip"
    if m.cip.name.lower() != expected.lower():
        return Failure("cip_name_mismatch",
                       f"cip name {m.cip.name!r} does not match PolicyID ({expected!r})")
    return None


def validate_version_greater(m: Manifest, deployed_version_ex: str | None) -> Failure | None:
    try:
        pushed = _ver_tuple(m.version_ex)
    except ValueError as e:
        return Failure("bad_version", str(e))
    if deployed_version_ex is None:
        return None  # nothing deployed yet -> any valid version is acceptable
    try:
        deployed = _ver_tuple(deployed_version_ex)
    except ValueError as e:
        return Failure("bad_version", f"deployed {e}")
    if pushed <= deployed:
        return Failure("stale_version",
                       f"version_ex {m.version_ex} <= currently deployed {deployed_version_ex}")
    return None


def validate_file_hashes(m: Manifest, inbox_dir: str | Path) -> Failure | None:
    inbox = Path(inbox_dir)
    for label, entry in (("policy_xml", m.policy_xml), ("cip", m.cip)):
        p = inbox / entry.name
        if not p.is_file():
            return Failure("missing_file", f"{label} file not found in inbox: {entry.name}")
        if flat_sha256(p).lower() != entry.sha256.lower():
            return Failure("hash_mismatch", f"{label} sha256 mismatch for {entry.name}")
    return None


def validate_selfprotect(policy_doc, install_root, *, dotnet_root=None,
                         extra_paths=None) -> Failure | None:
    missing = selfprotect.missing_required_paths(
        policy_doc, install_root, dotnet_root=dotnet_root, extra_paths=extra_paths)
    if missing:
        return Failure("selfprotect_uncovered",
                       f"policy is missing required self-protect FilePath rules: {missing}")
    return None


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #

def validate_all(m: Manifest, inbox_dir: str | Path, *, deployed_version_ex: str | None,
                 install_root, dotnet_root=None, extra_paths=None) -> list[Failure]:
    """Run every validator and return the list of failures (empty == accept).
    The policy XML is parsed from the inbox for the self-protect coverage check."""
    failures: list[Failure] = []
    for check in (
        validate_schema_version(m),
        validate_cip_name_matches_policy_id(m),
        validate_version_greater(m, deployed_version_ex),
        validate_file_hashes(m, inbox_dir),
    ):
        if check is not None:
            failures.append(check)

    policy_path = Path(inbox_dir) / m.policy_xml.name
    if policy_path.is_file():
        try:
            doc = ET.parse(policy_path)
        except ET.ParseError as e:
            failures.append(Failure("policy_xml_parse", f"unparseable policy XML: {e}"))
        else:
            sp_fail = validate_selfprotect(
                doc, install_root, dotnet_root=dotnet_root, extra_paths=extra_paths)
            if sp_fail is not None:
                failures.append(sp_fail)
    # a genuinely missing policy file is already reported by validate_file_hashes.
    return failures
