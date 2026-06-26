"""Cloud Bridge — connects the DLP agent to the Management Console.

Responsibilities:
- First-time registration (POST /api/v1/agents/register)
- Periodic heartbeat (PATCH /api/v1/agents/{id}/heartbeat)
- Policy translation: server format → local analyzer/policies.yaml format
- Atomic write of policies.yaml on heartbeat response
- Log sync: tail events.jsonl + dlp-agent.log → POST /api/v1/agents/{id}/logs
- Violation reporting: POST /api/v1/violation-logs/

All HTTP uses stdlib urllib.request — no new dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from orchestrator.config import OrchestratorConfig

log = logging.getLogger(__name__)


def translate_policies(server_policies: list[dict]) -> dict:
    """Convert server policy format to local analyzer/policies.yaml format.

    Server now uses the same fields as local policies.yaml:
    - type, patterns, keywords, channels, action, context_words, context_range
    So translation is mostly a pass-through with minor cleanup.
    """
    local_policies = []
    for p in server_policies:
        if not p.get("is_active", True):
            continue
        local: dict[str, Any] = {
            "id": str(p["id"]),
            "name": p.get("name", ""),
            "channels": p.get("channels", []),
            "action": p.get("action", "allow"),
            "type": p.get("type", "regex"),
        }
        # Pass through patterns/keywords based on rule type
        rule_type = local["type"]
        if rule_type == "regex":
            local["patterns"] = p.get("patterns", [])
        elif rule_type in ("denylist", "keyword"):
            local["keywords"] = p.get("keywords", [])
        # Context matching
        local["context_words"] = p.get("context_words", [])
        local["context_range"] = p.get("context_range", 0)
        local_policies.append(local)
    return {"policies": local_policies}


def _read_tail(path: Path, max_bytes: int = 50_000) -> str:
    """Read the last `max_bytes` of a file. Returns '' if file missing."""
    try:
        size = path.stat().st_size
        with open(path, encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read()
    except (OSError, FileNotFoundError):
        return ""


def _write_yaml_atomic(path: Path, content: str) -> None:
    """Write YAML content atomically via temp file + os.replace."""
    dir_ = path.parent
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_), suffix=".tmp", prefix="policies_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class CloudBridge:
    """Manages agent ↔ Management Console connectivity."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config
        self._base_url = config.server_url.rstrip("/")
        self._agent_id = config.server_agent_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_interval = config.server_heartbeat_interval
        self._log_sync_interval = config.server_log_sync_interval
        self._log_dir = self._resolve_log_dir()
        self._policies_file = Path(config.policies_file)
        if not self._policies_file.is_absolute():
            self._policies_file = Path(__file__).parent.parent / self._policies_file
        # Violation queue (bounded, non-blocking)
        self._violation_queue: queue.Queue[dict] | None = None
        self._violation_thread: threading.Thread | None = None

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def is_enabled(self) -> bool:
        return self._config.server_enabled and bool(self._base_url)

    def _resolve_log_dir(self) -> Path:
        programdata = os.environ.get("PROGRAMDATA", "C:\\ProgramData")
        return Path(programdata) / "DLP" / "logs"

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: int = 10,
    ) -> tuple[int, dict | str]:
        """Low-level HTTP request. Returns (status_code, parsed_body)."""
        url = f"{self._base_url}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw
        except urllib.error.HTTPError as exc:
            body_text = ""
            try:
                body_text = exc.read().decode("utf-8")
            except Exception:
                pass
            log.warning("HTTP %s %s → %d: %s", method, path, exc.code, body_text[:200])
            return exc.code, body_text
        except (urllib.error.URLError, OSError, socket.timeout) as exc:
            log.warning("HTTP %s %s failed: %s", method, path, exc)
            return 0, str(exc)

    def _get(self, path: str, timeout: int = 10) -> tuple[int, dict | str]:
        return self._request("GET", path, timeout=timeout)

    def _post(self, path: str, body: dict, timeout: int = 10) -> tuple[int, dict | str]:
        return self._request("POST", path, body=body, timeout=timeout)

    def _patch(self, path: str, body: dict | None = None, timeout: int = 10) -> tuple[int, dict | str]:
        return self._request("PATCH", path, body=body, timeout=timeout)

    # ── Registration ──────────────────────────────────────────────────────

    def ensure_registered(self) -> str:
        """Ensure we have a valid agent_id. Returns the agent_id.

        Flow:
        1. If agent_id already set → try heartbeat; if 200 → use it; if 404 →
           re-register; on any *other* (transient) outcome — network down, 5xx,
           auth — KEEP the configured id and return it so the heartbeat loop can
           retry. We must NOT fall through to /register in that case: /register is
           admin-JWT-guarded, so an agent call gets 403, which would drop us to
           standalone until the next service restart.
        2. If agent_id empty → register by hostname
        3. If hostname conflict (400) → lookup by hostname
        """
        if self._agent_id:
            # Verify it still exists on the server
            status, _ = self._patch(
                f"/api/v1/agents/{self._agent_id}/heartbeat"
            )
            if status == 200:
                log.info("Cloud bridge: agent_id %s verified", self._agent_id)
                return self._agent_id
            if status == 404:
                log.warning("Agent %s not found on server, re-registering", self._agent_id)
                self._agent_id = ""
            else:
                # Transient failure (network unreachable / 5xx / auth): trust the
                # admin-configured agent_id and let the heartbeat loop retry rather
                # than falling back to the admin-only /register (which would 403).
                log.warning(
                    "Cloud bridge: heartbeat for configured agent_id %s returned %s; "
                    "keeping it and retrying via the heartbeat loop",
                    self._agent_id, status,
                )
                return self._agent_id

        hostname = socket.gethostname()
        status, resp = self._post(
            "/api/v1/agents/register",
            {"hostname": hostname, "status": "inactive"},
        )
        if status == 201 and isinstance(resp, dict) and "id" in resp:
            self._agent_id = resp["id"]
            log.info("Cloud bridge: registered as agent_id=%s (hostname=%s)", self._agent_id, hostname)
            return self._agent_id

        if status == 400:
            # Hostname conflict — agent may have lost its config
            log.info("Hostname '%s' exists on server, looking up existing agent", hostname)
            return self._lookup_by_hostname(hostname)

        log.error("Cloud bridge: registration failed (status=%s), running standalone", status)
        return ""

    def _lookup_by_hostname(self, hostname: str) -> str:
        status, resp = self._get(f"/api/v1/agents/?search={hostname}")
        if status == 200 and isinstance(resp, dict):
            items = resp.get("items", [])
            for item in items:
                if item.get("hostname") == hostname:
                    self._agent_id = item["id"]
                    log.info("Cloud bridge: found existing agent_id=%s for hostname=%s", self._agent_id, hostname)
                    return self._agent_id
        log.error("Cloud bridge: could not find agent by hostname '%s'", hostname)
        return ""

    # ── Heartbeat ─────────────────────────────────────────────────────────

    def do_heartbeat(self) -> bool:
        """Send heartbeat, get policies, translate and apply. Returns True on success."""
        if not self._agent_id:
            return False

        status, resp = self._patch(
            f"/api/v1/agents/{self._agent_id}/heartbeat",
            timeout=15,
        )
        if status != 200:
            log.warning("Heartbeat failed (status=%s)", status)
            if status == 404:
                log.warning("Agent not found on server, attempting re-register")
                self._agent_id = ""
                self.ensure_registered()
            return False

        # resp is an AgentResponse dict with 'policies' list
        if not isinstance(resp, dict):
            log.warning("Heartbeat returned non-dict response")
            return False

        policies = resp.get("policies", [])
        self._apply_policies(policies)
        return True

    def _apply_policies(self, server_policies: list[dict]) -> None:
        """Translate server policies and write to local policies.yaml atomically."""
        local = translate_policies(server_policies)
        header = (
            "# Auto-generated by DLP Cloud Bridge — local edits will be overwritten\n"
            "# Source: Management Console (heartbeat)\n"
        )
        try:
            import re
            import yaml
            content = header + yaml.dump(
                local,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )
            # Inline channels list: "channels:\n- a\n- b" → "channels: [a, b]"
            def _inline_list(match):
                items = [l.strip() for l in match.group(1).splitlines() if l.strip().startswith("- ")]
                return "channels: [" + ", ".join(l.lstrip("- ") for l in items) + "]\n"
            content = re.sub(
                r"channels:\n((?:  - .+\n?)+)",
                _inline_list,
                content,
            )
            _write_yaml_atomic(self._policies_file, content)
            log.info(
                "Cloud bridge: applied %d policies from server",
                len(local.get("policies", [])),
            )
        except Exception:
            log.exception("Cloud bridge: failed to write policies.yaml")

    # ── Log Sync ──────────────────────────────────────────────────────────

    def do_log_sync(self) -> bool:
        """Push tails of events.jsonl + dlp-agent.log to server."""
        if not self._agent_id:
            return False

        events_path = self._log_dir / "events.jsonl"
        agent_log_path = self._log_dir / "dlp-agent.log"

        events_tail = _read_tail(events_path, max_bytes=50_000)
        agent_log_tail = _read_tail(agent_log_path, max_bytes=50_000)

        if not events_tail and not agent_log_tail:
            return True  # nothing to sync

        status, _ = self._post(
            f"/api/v1/agents/{self._agent_id}/logs",
            {
                "events_tail": events_tail,
                "agent_log_tail": agent_log_tail,
            },
            timeout=15,
        )
        if status not in (200, 201):
            log.warning("Log sync failed (status=%s)", status)
            return False
        return True

    # ── Violation Reporting ───────────────────────────────────────────────

    def report_violation(self, violation: dict) -> None:
        """Non-blocking violation report. Puts into bounded queue."""
        if self._violation_queue is None:
            return
        try:
            self._violation_queue.put_nowait(violation)
        except Exception:
            log.warning("Violation queue full, dropping event")

    def _violation_worker(self) -> None:
        """Background thread consuming violation queue and POSTing to server."""
        while not self._stop.is_set():
            try:
                violation = self._violation_queue.get(timeout=1.0)
            except Exception:
                continue
            try:
                status, _ = self._post(
                    "/api/v1/violation-logs/",
                    violation,
                    timeout=5,
                )
                if status not in (200, 201):
                    log.warning("Violation report failed (status=%s)", status)
            except Exception:
                log.warning("Violation report error", exc_info=True)

    # ── Background Thread ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start the heartbeat + log sync background thread."""
        if not self.is_enabled:
            log.info("Cloud bridge: disabled (standalone mode)")
            return

        # Register first (blocking, on calling thread)
        agent_id = self.ensure_registered()
        if not agent_id:
            log.warning("Cloud bridge: registration failed, falling back to standalone")
            return

        # Write agent_id back to config if it was just registered
        if not self._config.server_agent_id:
            self._persist_agent_id(agent_id)

        # Start violation queue worker
        self._violation_queue = queue.Queue(maxsize=1000)
        self._violation_thread = threading.Thread(
            target=self._violation_worker, daemon=True, name="cloud-violation"
        )
        self._violation_thread.start()

        # Start heartbeat + log sync thread
        self._thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="cloud-bridge"
        )
        self._thread.start()
        log.info(
            "Cloud bridge: started (agent_id=%s, heartbeat=%ds, log_sync=%ds)",
            self._agent_id, self._heartbeat_interval, self._log_sync_interval,
        )

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._stop.set()

    def _heartbeat_loop(self) -> None:
        last_log_sync = 0.0
        while not self._stop.is_set():
            try:
                self.do_heartbeat()
            except Exception:
                log.exception("Cloud bridge: heartbeat error")

            # Log sync at a lower frequency than heartbeat
            now = time.monotonic()
            if now - last_log_sync >= self._log_sync_interval:
                try:
                    self.do_log_sync()
                except Exception:
                    log.exception("Cloud bridge: log sync error")
                last_log_sync = now

            self._stop.wait(timeout=self._heartbeat_interval)

    def _persist_agent_id(self, agent_id: str) -> None:
        """Write agent_id back to config.yaml so future restarts skip register."""
        if self._config.server_agent_id:
            return  # already set
        config_path = Path(__file__).parent.parent / "config.yaml"
        try:
            import yaml
            with open(config_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            server_section = raw.get("server", {})
            server_section["agent_id"] = agent_id
            raw["server"] = server_section
            _write_yaml_atomic(config_path, yaml.dump(
                raw, sort_keys=False, allow_unicode=True, default_flow_style=False
            ))
            self._config.server_agent_id = agent_id
            log.info("Cloud bridge: persisted agent_id=%s to config.yaml", agent_id)
        except Exception:
            log.exception("Cloud bridge: failed to persist agent_id")
