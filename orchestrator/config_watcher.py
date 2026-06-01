"""Watchdog FileSystemWatcher on config.yaml.

On change → parse the yaml → invoke an on_change callback with the new dict.
Mirrors the shape of policy_manager._ReloadHandler (on_modified + on_moved +
on_created with debounce) so atomic-save editors are handled identically.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 0.2


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "ConfigWatcher") -> None:
        self._watcher = watcher
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._target_name = Path(watcher.yaml_path).name

    def on_modified(self, event) -> None:
        if Path(event.src_path).name == self._target_name:
            self._schedule()

    def on_moved(self, event) -> None:
        # Atomic-save editors (write-temp + rename) fire on_moved with the
        # destination path equal to the watched file.
        if Path(getattr(event, "dest_path", "")).name == self._target_name:
            self._schedule()

    def on_created(self, event) -> None:
        if Path(event.src_path).name == self._target_name:
            self._schedule()

    def _schedule(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(_DEBOUNCE_SECONDS, self._watcher._fire)
            self._timer.daemon = True
            self._timer.start()


class ConfigWatcher:
    """Debounced FileSystemWatcher on config.yaml.

    Note on coexistence with PolicyManager's observer: PolicyManager watches
    `analyzer/` (its policies.yaml's parent). This watcher watches the repo
    root (config.yaml's parent). Disjoint directories — no cross-fire. Even if
    these files ever co-locate, both handlers filter by exact filename.
    """

    def __init__(
        self,
        yaml_path: str | Path,
        on_change: Callable[[dict], None],
    ) -> None:
        self.yaml_path = str(Path(yaml_path).resolve())
        self._on_change = on_change
        self._observer: Observer | None = None

    def start(self) -> None:
        self._observer = Observer()
        handler = _Handler(self)
        watch_dir = str(Path(self.yaml_path).parent)
        self._observer.schedule(handler, watch_dir, recursive=False)
        self._observer.daemon = True
        self._observer.start()
        log.info("Config watcher watching %s", self.yaml_path)

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None

    def _fire(self) -> None:
        try:
            with open(self.yaml_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            log.error("Config reload failed (parse/IO error); keeping old config: %s", exc)
            return
        if not isinstance(raw, dict):
            log.error("Config reload skipped: top-level YAML is not a mapping (%s)", type(raw).__name__)
            return
        try:
            self._on_change(raw)
        except Exception:  # noqa: BLE001 — log + swallow; do not crash the watcher thread
            log.exception("Config on_change callback raised")
