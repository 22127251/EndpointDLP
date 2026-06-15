"""Agent ↔ Management-Server synchronisation daemon.

Runs as a background thread inside the orchestrator.  Responsible for:
  1. Registering the agent with the server on first contact.
  2. Sending periodic heartbeats.
  3. Pulling policies + config from the server when the hash changes.
  4. Pushing local log files (events.jsonl, dlp-agent.log) to the server.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from orchestrator.config import OrchestratorConfig

log = logging.getLogger(__name__)

# ── defaults ────────────────────────────────────────────────────────────────
_DEFAULT_SYNC_INTERVAL = 60        # seconds between heartbeat rounds
_DEFAULT_HEARTBEAT_TIMEOUT = 10    # HTTP timeout for heartbeat/register
_DEFAULT_PULL_TIMEOUT = 15         # HTTP timeout for config pull
_DEFAULT_PUSH_TIMEOUT = 30         # HTTP timeout for log push
_MAX_PUSH_BACKOFF = 300            # cap for exponential back-off on push fail


def _file_hash(path: Path) -> str:
    """SHA-256 of a file (first 16 hex chars). Returns '' if missing."""
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _hostname() -> str:
    return platform.node()


class ServerSync:
    """Background thread that keeps the agent registered and synchronised."""

    def __init__(self, config: OrchestratorConfig) -> None:
        server_cfg = config.raw.get("server", {})
        self._server_url: str = server_cfg.get("url", "").rstrip("/")
        self._agent_key: str = server_cfg.get("agent_key", "")
        self._sync_interval: int = server_cfg.get(
            "sync_interval_seconds", _DEFAULT_SYNC_INTERVAL
        )
        self._log_push_interval: int = server_cfg.get(
            "log_push_interval_seconds", _DEFAULT_SYNC_INTERVAL
        )
        self._enabled: bool = server_cfg.get("enabled", False)

        # State persisted across heartbeats
        self._agent_id: str = server_cfg.get("agent_id", "")
        self._last_policies_hash: str = ""
        self._last_config_hash: str = ""
        self._push_backoff: float = 1.0

        # Log directory
        log_dir_str = config.raw.get("paths", {}).get("log_dir", "")
        if log_dir_str:
            self._log_dir = Path(log_dir_str)
        else:
            self._log_dir = Path(os.environ.get(
                "PROGRAMDATA", r"C:\ProgramData"
            )) / "DLP" / "logs"

        # Stop signal
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ── public API ──────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._server_url)

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def start(self) -> None:
        if not self.enabled:
            log.info("ServerSync disabled or no server_url configured; skipping.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="server-sync"
        )
        self._thread.start()
        log.info(
            "ServerSync started (server=%s, interval=%ds)",
            self._server_url, self._sync_interval,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("ServerSync stopped.")

    # ── main loop ───────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        # First: register if needed, then loop heartbeat + push
        if not self._agent_id:
            self._register()

        last_push_time = 0.0
        while not self._stop_event.is_set():
            try:
                self._heartbeat()
            except Exception:
                log.exception("ServerSync heartbeat failed")

            # Log push on a separate cadence (or same, but tracked independently)
            now = time.time()
            if now - last_push_time >= self._log_push_interval:
                try:
                    self._push_logs()
                    last_push_time = now
                except Exception:
                    log.exception("ServerSync log push failed")

            self._stop_event.wait(timeout=self._sync_interval)

    # ── register ────────────────────────────────────────────────────────

    def _register(self) -> None:
        url = f"{self._server_url}/api/v1/agents/register"
        payload = {
            "id": None,  # server generates UUID
            "hostname": _hostname(),
            "status": "active",
            "description": f"Auto-registered by {__name__}",
        }
        headers = {"X-Agent-Key": self._agent_key}
        try:
            resp = requests.post(
                url, json=payload, headers=headers,
                timeout=_DEFAULT_HEARTBEAT_TIMEOUT,
            )
            if resp.status_code == 201:
                data = resp.json()
                self._agent_id = data.get("id", "")
                log.info(
                    "Registered with server. agent_id=%s hostname=%s",
                    self._agent_id, _hostname(),
                )
                self._save_agent_id()
            elif resp.status_code == 409:
                # Agent with this hostname already exists – try to find it
                log.info("Agent already registered; fetching agent list.")
                self._find_existing_agent()
            else:
                log.warning(
                    "Register failed: %s %s", resp.status_code, resp.text[:200]
                )
        except requests.RequestException as exc:
            log.warning("Register network error: %s", exc)

    def _find_existing_agent(self) -> None:
        """Look up our hostname in the agent list to recover agent_id."""
        url = f"{self._server_url}/api/v1/agents/"
        headers = {
            "X-Agent-Key": self._agent_key,
            "Authorization": f"Bearer {self._agent_key}",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=_DEFAULT_HEARTBEAT_TIMEOUT)
            if resp.status_code == 200:
                for agent in resp.json().get("items", []):
                    if agent.get("hostname") == _hostname():
                        self._agent_id = agent["id"]
                        self._save_agent_id()
                        log.info("Found existing agent_id=%s", self._agent_id)
                        return
        except requests.RequestException:
            pass
        log.warning("Could not find existing agent for hostname=%s", _hostname())

    def _save_agent_id(self) -> None:
        """Persist agent_id back into config.yaml so it survives restarts."""
        config_path = Path(__file__).parent.parent / "config.yaml"
        try:
            import yaml
            with open(config_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            raw.setdefault("server", {})["agent_id"] = self._agent_id
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(raw, f, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)
            log.debug("Persisted agent_id=%s to %s", self._agent_id, config_path)
        except Exception:
            log.exception("Could not persist agent_id to config.yaml")

    # ── heartbeat ───────────────────────────────────────────────────────

    def _heartbeat(self) -> None:
        if not self._agent_id:
            log.warning("No agent_id; skipping heartbeat.")
            return

        url = f"{self._server_url}/api/v1/agents/{self._agent_id}/heartbeat"
        headers = {"X-Agent-Key": self._agent_key}

        # Compute local hashes so server can tell us if something changed
        policies_file = Path(
            __file__).parent.parent / "analyzer" / "policies.yaml"
        config_file = Path(__file__).parent.parent / "config.yaml"
        params = {
            "format": "json",
            "policies_hash": _file_hash(policies_file),
            "config_hash": _file_hash(config_file),
        }

        try:
            resp = requests.patch(
                url, headers=headers, params=params,
                timeout=_DEFAULT_HEARTBEAT_TIMEOUT,
            )
            if resp.status_code == 200:
                self._on_heartbeat_response(resp.json())
            elif resp.status_code == 404:
                log.warning("Agent not found on server; re-registering.")
                self._agent_id = ""
                self._register()
            else:
                log.warning(
                    "Heartbeat failed: %s %s", resp.status_code, resp.text[:200]
                )
        except requests.RequestException as exc:
            log.warning("Heartbeat network error: %s", exc)

    def _on_heartbeat_response(self, data: dict) -> None:
        """Process server heartbeat response → pull policies/config if changed."""
        server_policies_hash = data.get("policies_hash", "")
        server_config_hash = data.get("config_hash", "")

        if server_policies_hash and server_policies_hash != self._last_policies_hash:
            log.info(
                "Server policies_hash changed (%s → %s); pulling policies.",
                self._last_policies_hash or "(none)", server_policies_hash,
            )
            self._pull_policies()
            self._last_policies_hash = server_policies_hash

        if server_config_hash and server_config_hash != self._last_config_hash:
            log.info(
                "Server config_hash changed (%s → %s); pulling config.",
                self._last_config_hash or "(none)", server_config_hash,
            )
            self._pull_config()
            self._last_config_hash = server_config_hash

    # ── pull policies & config ──────────────────────────────────────────

    def _pull_policies(self) -> None:
        """Download policies.yaml from the server and overwrite local copy."""
        url = f"{self._server_url}/api/v1/agents/{self._agent_id}/config"
        headers = {"X-Agent-Key": self._agent_key}
        params = {"format": "yaml"}

        try:
            resp = requests.get(
                url, headers=headers, params=params,
                timeout=_DEFAULT_PULL_TIMEOUT,
            )
            if resp.status_code == 200:
                remote_data = resp.json()
                policies = remote_data.get("policies", [])
                if policies:
                    self._write_policies_yaml(policies)
                    log.info("Policies pulled and written (%d rules).", len(policies))
                else:
                    log.info("Server returned 0 active policies; keeping local.")
            else:
                log.warning(
                    "Policy pull failed: %s %s", resp.status_code, resp.text[:200]
                )
        except requests.RequestException as exc:
            log.warning("Policy pull network error: %s", exc)

    def _write_policies_yaml(self, policies: list[dict]) -> None:
        """Convert server PolicyResponse list → local policies.yaml format."""
        import yaml

        local_rules: list[dict] = []
        for p in policies:
            if not p.get("is_active", True):
                continue
            rule: dict = p.get("rule", {})
            local_p: dict = {
                "id": str(p["id"]),
                "name": p.get("name", ""),
            }
            # Map channel
            channel = p.get("channel", "all")
            if channel == "all":
                local_p["channels"] = ["browser", "clipboard", "peripheral_storage"]
            else:
                local_p["channels"] = [channel]

            local_p["action"] = p.get("action", "block")
            local_p["type"] = p.get("rule_type", "regex")

            # patterns / keywords from rule dict
            if p.get("rule_type") == "regex":
                local_p["patterns"] = rule.get("patterns", [])
                if rule.get("pattern"):
                    local_p["patterns"].append(rule["pattern"])
            elif p.get("rule_type") == "keyword":
                local_p["keywords"] = rule.get("keywords", [])

            context_words = rule.get("context_words", [])
            if context_words:
                local_p["context_words"] = context_words
                local_p["context_range"] = rule.get("context_range", 120)

            local_rules.append(local_p)

        policies_file = Path(__file__).parent.parent / "analyzer" / "policies.yaml"
        with open(policies_file, "w", encoding="utf-8") as f:
            yaml.dump(
                {"policies": local_rules}, f,
                sort_keys=False, allow_unicode=True, default_flow_style=False,
            )

    def _pull_config(self) -> None:
        """Download config.json from the server and merge safe fields into local config.yaml."""
        url = f"{self._server_url}/api/v1/agents/{self._agent_id}/config"
        headers = {"X-Agent-Key": self._agent_key}
        params = {"format": "json"}

        try:
            resp = requests.get(
                url, headers=headers, params=params,
                timeout=_DEFAULT_PULL_TIMEOUT,
            )
            if resp.status_code == 200:
                remote_data = resp.json()
                # Merge server-provided settings into local config
                self._merge_config(remote_data)
                log.info("Config pulled and merged from server.")
            else:
                log.warning(
                    "Config pull failed: %s %s", resp.status_code, resp.text[:200]
                )
        except requests.RequestException as exc:
            log.warning("Config pull network error: %s", exc)

    def _merge_config(self, remote_data: dict) -> None:
        """Merge safe fields from remote config into local config.yaml.
        
        Only merges non-sensitive operational fields; pipe names and install
        paths are NOT overwritten (they're local-only).
        """
        import yaml

        config_path = Path(__file__).parent.parent / "config.yaml"
        with open(config_path, encoding="utf-8") as f:
            local = yaml.safe_load(f) or {}

        # Fields the server is allowed to push
        mergable_keys = [
            ("policies",),  # not a real config field, skip
            ("browser", "fail_behavior"),
            ("browser", "min_upload_size_bytes"),
            ("peripheral_storage", "fail_mode"),
            ("peripheral_storage", "controller_in_user_session"),
            ("pools",),
            ("limits",),
            ("service",),
        ]

        # Simple top-level merges from server's "config" section
        server_config = remote_data.get("config", {})
        for key in ["pools", "limits", "service"]:
            if key in server_config and isinstance(server_config[key], dict):
                local.setdefault(key, {})
                local[key].update(server_config[key])

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(local, f, sort_keys=False, allow_unicode=True,
                      default_flow_style=False)

    # ── push logs ───────────────────────────────────────────────────────

    def _push_logs(self) -> None:
        if not self._agent_id:
            return

        url = f"{self._server_url}/api/v1/agents/{self._agent_id}/logs"
        headers = {"X-Agent-Key": self._agent_key}

        files: dict = {}
        events_path = self._log_dir / "events.jsonl"
        agent_log_path = self._log_dir / "dlp-agent.log"

        if events_path.is_file():
            files["events_file"] = (
                "events.jsonl", open(events_path, "rb"), "application/jsonl"
            )
        if agent_log_path.is_file():
            files["agent_log_file"] = (
                "dlp-agent.log", open(agent_log_path, "rb"), "text/plain"
            )

        if not files:
            log.debug("No log files to push.")
            return

        try:
            resp = requests.post(
                url, files=files, headers=headers,
                timeout=_DEFAULT_PUSH_TIMEOUT,
            )
            if resp.status_code == 200:
                log.info("Logs pushed to server: %s", resp.json().get("uploaded", []))
                self._push_backoff = 1.0
            else:
                log.warning(
                    "Log push failed: %s %s", resp.status_code, resp.text[:200]
                )
                self._push_backoff = min(
                    self._push_backoff * 2, _MAX_PUSH_BACKOFF
                )
        except requests.RequestException as exc:
            log.warning("Log push network error: %s", exc)
            self._push_backoff = min(
                self._push_backoff * 2, _MAX_PUSH_BACKOFF
            )
        finally:
            # Close file handles
            for f_tuple in files.values():
                f_tuple[1].close()
