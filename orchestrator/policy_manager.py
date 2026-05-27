from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from analyzer.engine import DLPEngine
from analyzer.extractor import extract_tabular, extract_text, is_tabular
from orchestrator.config import OrchestratorConfig

log = logging.getLogger(__name__)


class _ReloadHandler(FileSystemEventHandler):
    def __init__(self, manager: PolicyManager) -> None:
        self._manager = manager
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def on_modified(self, event) -> None:
        if Path(event.src_path).name != "policies.yaml":
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(0.5, self._manager._reload_engine)
            self._timer.daemon = True
            self._timer.start()


class PolicyManager:
    def __init__(self, config: OrchestratorConfig) -> None:
        repo_root = Path(__file__).parent.parent
        self._policies_file = str(repo_root / config.policies_file)
        log.info("Loading policies from %s", self._policies_file)
        self._engine = DLPEngine(self._policies_file)
        log.info("DLPEngine ready.")

        self._observer = Observer()
        handler = _ReloadHandler(self)
        watch_dir = str(Path(self._policies_file).resolve().parent)
        self._observer.schedule(handler, watch_dir, recursive=False)
        self._observer.daemon = True
        self._observer.start()

    def get_engine(self) -> DLPEngine:
        return self._engine

    def _reload_engine(self) -> None:
        try:
            new_engine = DLPEngine(self._policies_file)
            self._engine = new_engine  # atomic in CPython (GIL)
            log.info("Policies reloaded from %s", self._policies_file)
        except Exception as exc:
            log.error("Policy reload failed, keeping old engine: %s", exc)

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()

    def analyze(
        self,
        channel: str,
        kind: str,
        text: str | None = None,
        file_path: str | None = None,
        req_id: str = "",
    ) -> tuple[str, list]:
        engine = self._engine  # snapshot before any reload can swap it
        if kind == "text":
            body = text or ""
            result = engine.analyze(body, channel)
            content_label = (
                f"size={len(body)} hash={hashlib.sha256(body.encode()).hexdigest()[:8]}"
            )
        elif kind == "file":
            if not file_path:
                log.error("kind=file but no file_path provided; failing closed.")
                return "BLOCK", []
            filename = os.path.basename(file_path)
            size = os.path.getsize(file_path)
            content_label = f"file={filename} size={size}"
            try:
                if is_tabular(file_path):
                    result = engine.analyze_tabular(extract_tabular(file_path), channel)
                else:
                    result = engine.analyze(extract_text(file_path), channel)
            finally:
                try:
                    os.unlink(file_path)
                except OSError as e:
                    log.warning("Could not delete temp file %s: %s", file_path, e)
        else:
            log.error("Unknown kind=%r; failing closed.", kind)
            return "BLOCK", []

        action = result.applied_action
        if action == "block":
            log.warning("BLOCK req=%s channel=%s %s elapsed=%.1fms violations=[%s]",
                        req_id, channel, content_label, result.elapsed_ms,
                        _fmt_violations(result.violations))
            return "BLOCK", result.violations
        elif action == "allow_log":
            log.info("ALLOW(logged) req=%s channel=%s %s elapsed=%.1fms violations=[%s]",
                     req_id, channel, content_label, result.elapsed_ms,
                     _fmt_violations(result.violations))
            return "ALLOW", result.violations
        else:
            log.debug("ALLOW req=%s channel=%s %s elapsed=%.1fms",
                      req_id, channel, content_label, result.elapsed_ms)
            return "ALLOW", result.violations


def _fmt_violations(violations: list) -> str:
    return " ".join(
        f"{v.policy_id}({v.action})×{len(v.matches)}"
        for v in violations
    )
