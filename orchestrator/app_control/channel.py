"""App Control channel facade — owns the inbox watcher, deployer, and event
forwarder, and is the single object ``run_core`` starts/stops (Phase AC-3).

Lives inside the orchestrator as daemon threads (it needs LocalSystem to deploy to
``System32\\CodeIntegrity`` and to ``EvtSubscribe`` the CI log). It never touches
the analyzer. Directories resolve from ``%PROGRAMDATA%`` exactly like
``installer.py``/``supervisor.py``; ``start()`` creates them if absent, so the
channel is self-sufficient before the AC-5 installer step exists.
"""
from __future__ import annotations

import logging
import threading

from . import paths
from . import policy_xml as px
from .deployer import Deployer
from .event_forwarder import EventForwarder
from .inbox import InboxWatcher

log = logging.getLogger("orchestrator.app_control.channel")


class AppControlChannel:
    def __init__(self, config) -> None:
        self._config = config
        # Dirs/status/roots resolve via paths.py — the single source of truth the
        # AC-4 builder shares, so `dlp-ctl appcontrol apply` writes the inbox this
        # watcher reads.
        self._inbox_dir = paths.inbox_dir(config)
        self._rejected_dir = paths.rejected_dir(config)
        self._staging_dir = paths.staging_dir(config)
        self._status_path = paths.status_path(config)
        self._install_root = paths.install_root(config)
        self._dotnet_root = paths.dotnet_root(config)
        self._extra_paths = paths.extra_paths(config)

        self._poll_seconds = config.app_control_poll_seconds
        self._reconcile_interval = config.app_control_reconcile_interval_seconds
        self._forward = config.app_control_forward_block_events
        self._policy_id = px.get_policy_id(px.load_base_policy())

        self._deployer: Deployer | None = None
        self._watcher: InboxWatcher | None = None
        self._forwarder: EventForwarder | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            for d in (self._inbox_dir, self._rejected_dir, self._staging_dir):
                d.mkdir(parents=True, exist_ok=True)
            self._deployer = Deployer(status_path=self._status_path, policy_id=self._policy_id)
            self._watcher = InboxWatcher(
                inbox_dir=self._inbox_dir, rejected_dir=self._rejected_dir,
                deployer=self._deployer, base_policy_id=self._policy_id,
                install_root=self._install_root, dotnet_root=self._dotnet_root,
                extra_paths=self._extra_paths, poll_seconds=self._poll_seconds,
                reconcile_interval_seconds=self._reconcile_interval)
            self._thread = threading.Thread(
                target=self._watcher.run, daemon=True, name="appcontrol-inbox")
            self._thread.start()
            if self._forward:
                self._start_forwarder()
            self._started = True
            log.info("App Control channel started (inbox=%s, policy=%s)",
                     self._inbox_dir, self._policy_id)

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            try:
                if self._watcher is not None:
                    self._watcher.stop()
                if self._thread is not None:
                    self._thread.join(timeout=5.0)
                if self._forwarder is not None:
                    self._forwarder.stop()
            except Exception:  # noqa: BLE001 — stop must never raise
                log.exception("App Control channel stop failed")
            self._started = False
            log.info("App Control channel stopped.")

    def _start_forwarder(self) -> None:
        self._forwarder = EventForwarder(
            policy_id=self._policy_id,
            on_block=self._deployer.note_block if self._deployer else None)
        self._forwarder.start()

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        # Reconcile on-demand so `dlp-ctl status` always reflects the live citool
        # state (authoritative), regardless of the watcher's throttle. Best-effort:
        # a citool failure is recorded in last_error, never raised.
        if self._deployer is not None:
            try:
                self._deployer.reconcile()
            except Exception:  # noqa: BLE001
                log.exception("status: reconcile failed")
        st = self._deployer.read_status() if self._deployer else {}
        return {
            "enabled": self._config.app_control_enabled,
            "running": self._started,
            "policy_guid": st.get("policy_guid"),
            "version_ex": st.get("version_ex"),
            "deployed_at": st.get("deployed_at"),
            "last_error": st.get("last_error"),
            "blocks": st.get("blocks", {"enforce": 0, "audit": 0}),
            "last_block_at": st.get("last_block_at"),
            "forwarder": bool(self._forwarder and self._forwarder.running),
            "pending_inbox": self._watcher.pending_count() if self._watcher else 0,
            "rejected_count": self._watcher.rejected_count if self._watcher else 0,
            "inbox_dir": str(self._inbox_dir),
        }

    # -- emergency disable (admin-pipe `appcontrol_disable`) ----------------

    def disable(self) -> dict:
        """Remove our deployed policy via the deployer (``citool --remove-policy``
        + neutralizer fallback). Returns ``{"removed": bool, ...status}``. Driven by
        AC-4's ``dlp-ctl appcontrol disable`` over the admin-pipe; never raises."""
        if self._deployer is None:
            return {"removed": False, "error": "deployer not started"}
        try:
            removed = self._deployer.remove()
        except Exception as exc:  # noqa: BLE001 — must not crash the admin loop
            log.exception("app_control disable failed")
            return {"removed": False, "error": str(exc)}
        return {"removed": removed, **self.status()}

    # -- hot-reload (bounded; dir changes need a restart, like data_pipe) ---

    def apply_config(self, new_raw: dict) -> None:
        ac = (new_raw or {}).get("app_control", {}) or {}
        new_poll = ac.get("poll_seconds", self._poll_seconds)
        if self._watcher is not None and new_poll != self._poll_seconds:
            self._poll_seconds = new_poll
            self._watcher.set_poll_seconds(new_poll)
            log.info("app_control poll_seconds -> %s", new_poll)

        new_forward = ac.get("forward_block_events", self._forward)
        if new_forward != self._forward:
            self._forward = new_forward
            if new_forward and self._forwarder is None and self._started:
                self._start_forwarder()
            elif not new_forward and self._forwarder is not None:
                self._forwarder.stop()
                self._forwarder = None
            log.info("app_control forward_block_events -> %s", new_forward)
