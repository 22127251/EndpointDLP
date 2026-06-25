"""Deploy / remove a compiled WDAC policy via ``citool``, and persist the
deployed-state record ``appcontrol_status.json`` (Phase AC-3).

The ``.cip`` arrives **already compiled** (parent decision 2) — this module never
calls ``ConvertFrom-CIPolicy``. It copies the binary into
``System32\\CodeIntegrity\\CIPolicies\\Active\\`` and applies it with
``citool --refresh``, confirming via ``citool --list-policies``.

Every citool call follows the AC-1-pinned recipe (``interceptors/app_control/
spike-results/RESULTS.md``): run **elevated** (the service is LocalSystem), pass
``--json`` for parseable output, and **redirect stdin** (citool prompts on stdin
even with ``--json``). The actual subprocess call is isolated behind an injectable
``runner`` so the unit tests stay OS-free.

Fail-safe invariant: a failed refresh never leaves a half-applied policy — the
previous on-disk ``.cip`` (if any) is restored, the previous status is kept, and
the caller is told it failed.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from orchestrator.events import record_app_control_event

from . import neutralizer as nz

log = logging.getLogger("orchestrator.app_control.deployer")

#: A citool runner: takes the argument list (without the leading ``citool``) and
#: returns ``(returncode, stdout_text)``. Injectable so tests never shell out.
Runner = Callable[[list], "tuple[int, str]"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def default_active_dir() -> Path:
    """``%SystemRoot%\\System32\\CodeIntegrity\\CIPolicies\\Active`` — where Windows
    loads multiple-format policies from."""
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    return Path(windir) / "System32" / "CodeIntegrity" / "CIPolicies" / "Active"


def citool_path() -> str:
    """Absolute path to ``citool.exe`` (``%SystemRoot%\\System32\\citool.exe``).

    A bare ``"citool"`` relies on the process PATH resolving System32 — which is not
    guaranteed inside a LocalSystem service subprocess. Falls back to the bare name
    if the file isn't found, so a non-standard layout still gets a chance."""
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    cand = os.path.join(windir, "System32", "citool.exe")
    return cand if os.path.isfile(cand) else "citool"


def default_runner(args: list) -> "tuple[int, str]":
    """Invoke ``citool`` (absolute path) with stdin closed (EOF satisfies citool's
    stdin prompt) and capture stdout. The orchestrator runs as LocalSystem, so no
    explicit elevation is needed here. Non-zero exits + stderr are logged so a
    failing citool in the service context is diagnosable from dlp-agent.log."""
    exe = citool_path()
    try:
        proc = subprocess.run(
            [exe, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
        )
    except OSError as exc:
        log.error("citool invocation failed (%s %s): %s", exe, args, exc)
        raise
    if proc.returncode != 0:
        log.warning("citool %s -> rc=%s stderr=%r", args, proc.returncode,
                    (proc.stderr or "")[:300])
    return proc.returncode, proc.stdout or ""


def _bare_guid(guid: str) -> str:
    """Lower-case GUID without braces (the form ``--list-policies`` reports)."""
    return guid.strip().strip("{}").lower()


def _empty_status() -> dict:
    return {
        "policy_guid": None,
        "version_ex": None,
        "deployed_at": None,
        "last_error": None,
        "last_error_at": None,
        "blocks": {"enforce": 0, "audit": 0},
        "last_block_at": None,
        "last_block": None,
    }


class Deployer:
    def __init__(self, *, status_path: str | Path, policy_id: str,
                 runner: Runner | None = None,
                 active_dir: str | Path | None = None,
                 neutralizer_cip: str | Path | None = None) -> None:
        self._status_path = Path(status_path)
        self._policy_id = policy_id                      # braced GUID (our base)
        self._runner: Runner = runner or default_runner
        self._active_dir = Path(active_dir) if active_dir else default_active_dir()
        self._neutralizer_cip = (
            Path(neutralizer_cip) if neutralizer_cip else nz.neutralizer_cip_path())
        self._lock = threading.Lock()

    # -- status store ------------------------------------------------------

    def read_status(self) -> dict:
        try:
            data = json.loads(self._status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return _empty_status()
        # Merge onto the default so older/partial files always have every key.
        base = _empty_status()
        base.update(data)
        if "blocks" not in data or not isinstance(data.get("blocks"), dict):
            base["blocks"] = {"enforce": 0, "audit": 0}
        return base

    def deployed_version_ex(self) -> str | None:
        return self.read_status().get("version_ex")

    def _write_status(self, status: dict) -> None:
        self._status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._status_path.with_suffix(self._status_path.suffix + ".tmp")
        tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
        os.replace(tmp, self._status_path)

    def _record_error(self, status: dict, msg: str) -> None:
        status["last_error"] = msg
        status["last_error_at"] = _now()
        self._write_status(status)

    # -- citool helpers ----------------------------------------------------

    def _refresh(self) -> bool:
        rc, out = self._runner(["--refresh", "--json"])
        obj = _parse_json(out)
        ok = obj.get("OperationResult") == 0
        if not ok:
            log.error("citool --refresh failed: rc=%s out=%r", rc, out[:300])
        return ok

    def _list_policies(self) -> list:
        rc, out = self._runner(["--list-policies", "--json"])
        obj = _parse_json(out)
        pols = obj.get("Policies")
        return pols if isinstance(pols, list) else []

    def _find_deployed(self) -> dict | None:
        bare = _bare_guid(self._policy_id)
        for p in self._list_policies():
            pid = str(p.get("PolicyID", "")).strip().lower()
            if pid == bare:
                return p
        return None

    # -- deploy ------------------------------------------------------------

    def deploy(self, push_dir: str | Path, manifest) -> bool:
        """Copy the push's ``.cip`` into Active\\ and refresh. Returns True on a
        confirmed deploy; on failure restores prior state and keeps the old status."""
        with self._lock:
            push_dir = Path(push_dir)
            cip_src = push_dir / manifest.cip.name
            dest = self._active_dir / manifest.cip.name
            status = self.read_status()
            backup: Path | None = None
            try:
                if not cip_src.is_file():
                    self._record_error(status, f"cip missing in push: {cip_src.name}")
                    return False
                self._active_dir.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    backup = dest.with_suffix(".cip.bak")
                    shutil.copy2(dest, backup)
                shutil.copy2(cip_src, dest)

                if not self._refresh():
                    self._rollback(dest, backup)
                    self._record_error(status, "citool --refresh returned non-zero")
                    record_app_control_event(event="deploy", outcome="failed",
                                             detail={"policy_guid": self._policy_id,
                                                     "version_ex": manifest.version_ex,
                                                     "reason": "refresh_failed"})
                    return False

                deployed = self._find_deployed()
                version = (deployed or {}).get("VersionString") or manifest.version_ex
                if deployed is None:
                    log.warning("refresh OK but policy %s not in --list-policies; "
                                "recording manifest version %s", self._policy_id, version)

                new_status = self.read_status()  # preserve block counters
                new_status.update({
                    "policy_guid": self._policy_id,
                    "version_ex": version,
                    "deployed_at": _now(),
                    "last_error": None,
                    "last_error_at": None,
                })
                self._write_status(new_status)
                if backup is not None:
                    backup.unlink(missing_ok=True)
                log.info("Deployed policy %s version %s", self._policy_id, version)
                record_app_control_event(event="deploy", outcome="ok",
                                         detail={"policy_guid": self._policy_id,
                                                 "version_ex": version})
                return True
            except Exception as exc:  # noqa: BLE001 — never propagate to the watcher
                log.exception("deploy failed")
                self._rollback(dest, backup)
                self._record_error(status, f"deploy exception: {exc}")
                return False

    def _rollback(self, dest: Path, backup: Path | None) -> None:
        """Restore the previous on-disk state after a failed refresh. The live
        policy never changed (refresh failed), so restoring the file is enough."""
        try:
            if backup is not None and backup.exists():
                shutil.copy2(backup, dest)
                backup.unlink(missing_ok=True)
            elif dest.exists():
                dest.unlink()  # nothing was deployed before — remove the new file
        except OSError:
            log.exception("rollback of %s failed", dest)

    # -- remove / neutralize ----------------------------------------------

    def remove(self) -> bool:
        """Remove our deployed policy. Primary path: ``citool --remove-policy``
        (no-reboot on 24H2+). Fallback: deploy the AllowAll neutralizer for
        immediate relief, then delete the active ``.cip`` (gone after reboot)."""
        with self._lock:
            dest = self._active_dir / f"{self._policy_id}.cip"
            try:
                rc, out = self._runner(["--remove-policy", self._policy_id, "--json"])
                if self._find_deployed() is None:
                    dest.unlink(missing_ok=True)
                    self._set_no_policy()
                    record_app_control_event(event="remove", outcome="ok",
                                             detail={"policy_guid": self._policy_id})
                    log.info("Removed policy %s (no reboot)", self._policy_id)
                    return True

                log.warning("remove-policy did not remove %s (rc=%s out=%r); "
                            "falling back to neutralizer", self._policy_id, rc, out[:200])
                return self._neutralize(dest)
            except Exception as exc:  # noqa: BLE001
                log.exception("remove failed")
                status = self.read_status()
                self._record_error(status, f"remove exception: {exc}")
                return False

    def _neutralize(self, dest: Path) -> bool:
        if not self._neutralizer_cip.is_file():
            log.error("neutralizer cip missing at %s; cannot disable", self._neutralizer_cip)
            return False
        self._active_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._neutralizer_cip, dest)   # overwrite our {GUID}.cip with allow-all
        if not self._refresh():
            self._record_error(self.read_status(), "neutralizer refresh failed")
            return False
        dest.unlink(missing_ok=True)                 # so it's gone after next boot
        self._set_no_policy()
        record_app_control_event(event="neutralize", outcome="ok",
                                 detail={"policy_guid": self._policy_id,
                                         "note": "allow-all applied live; removed at next boot"})
        log.info("Neutralized policy %s (allow-all live, gone after reboot)", self._policy_id)
        return True

    def _set_no_policy(self) -> None:
        status = self.read_status()
        # Block counters are per-deployment (the full history stays in events.jsonl),
        # so clearing the policy clears them too.
        status.update({"policy_guid": None, "version_ex": None, "deployed_at": None,
                       "last_error": None, "last_error_at": None,
                       "blocks": {"enforce": 0, "audit": 0},
                       "last_block_at": None, "last_block": None})
        self._write_status(status)

    # -- reconcile (self-heal the record vs live citool state) -------------

    def reconcile(self) -> bool:
        """Sync ``appcontrol_status.json`` with the live ``citool`` state. Returns
        True if it changed anything. Called on the watcher's first tick, throttled in
        the poll loop, and on-demand from ``dlp-ctl status`` — so the record
        self-heals after a service restart or an out-of-band ``citool`` change (e.g.
        a manual ``--remove-policy``).

        Strict: a citool failure (raise, error HRESULT, or unparseable output) is
        recorded in ``last_error`` and leaves the record untouched — it never clears a
        real deployment on a transient error, and the failure is visible via
        ``dlp-ctl status`` + dlp-agent.log."""
        with self._lock:
            try:
                rc, out = self._runner(["--list-policies", "--json"])
            except Exception as exc:  # noqa: BLE001 — never break the watcher
                log.exception("reconcile: citool invocation failed")
                self._record_error(self.read_status(), f"reconcile: citool failed: {exc}")
                return False
            obj = _parse_json(out)
            pols = obj.get("Policies")
            if not isinstance(pols, list):
                log.warning("reconcile: citool gave no policy list (rc=%s, out=%r)",
                            rc, out[:200])
                self._record_error(
                    self.read_status(),
                    f"reconcile: citool returned no policy list (rc={rc})")
                return False

            bare = _bare_guid(self._policy_id)
            deployed = next(
                (p for p in pols if str(p.get("PolicyID", "")).strip().lower() == bare),
                None)
            status = self.read_status()
            recorded = status.get("policy_guid")
            if deployed is None:
                if recorded is None:
                    return False
                self._set_no_policy()
                record_app_control_event(event="reconcile", outcome="cleared",
                                         detail={"policy_guid": recorded,
                                                 "note": "policy no longer deployed"})
                log.info("reconcile: cleared stale record for %s", recorded)
                return True

            live_ver = deployed.get("VersionString")
            if recorded == self._policy_id and status.get("version_ex") == live_ver:
                return False
            status.update({"policy_guid": self._policy_id, "version_ex": live_ver,
                           "last_error": None, "last_error_at": None})
            if status.get("deployed_at") is None:
                status["deployed_at"] = _now()
            self._write_status(status)
            record_app_control_event(event="reconcile", outcome="adopted",
                                     detail={"policy_guid": self._policy_id,
                                             "version_ex": live_ver})
            log.info("reconcile: adopted live policy %s v%s", self._policy_id, live_ver)
            return True

    # -- block accounting (called by the event forwarder) ------------------

    def note_block(self, outcome: str, detail: dict | None = None) -> None:
        """Bump the persisted block counters. ``outcome`` is ``"blocked"`` (3077
        enforce) or ``"audit"`` (3076)."""
        with self._lock:
            status = self.read_status()
            key = "audit" if outcome == "audit" else "enforce"
            status["blocks"][key] = int(status["blocks"].get(key, 0)) + 1
            status["last_block_at"] = _now()
            if detail:
                status["last_block"] = {k: detail.get(k) for k in ("file", "process")
                                        if detail.get(k)}
            self._write_status(status)


def _parse_json(out: str) -> dict:
    try:
        obj = json.loads(out)
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}
