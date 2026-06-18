from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from analyzer.engine import DLPEngine
from analyzer.extractor import ExtractionTooLarge, extract_tabular, extract_text, is_tabular
from orchestrator.config import OrchestratorConfig

log = logging.getLogger(__name__)


_RELOAD_DEBOUNCE_SECONDS = 0.1


class _ReloadHandler(FileSystemEventHandler):
    def __init__(self, manager: PolicyManager) -> None:
        self._manager = manager
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def on_modified(self, event) -> None:
        if Path(event.src_path).name == "policies.yaml":
            self._schedule_reload()

    def on_moved(self, event) -> None:
        # Atomic-save editors write to a temp file then rename onto policies.yaml,
        # which fires on_moved (not on_modified). Without this, hot-reload silently
        # misses every atomic save.
        if Path(getattr(event, "dest_path", "")).name == "policies.yaml":
            self._schedule_reload()

    def on_created(self, event) -> None:
        if Path(event.src_path).name == "policies.yaml":
            self._schedule_reload()

    def _schedule_reload(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(
                _RELOAD_DEBOUNCE_SECONDS, self._manager._reload_engine
            )
            self._timer.daemon = True
            self._timer.start()


class PolicyManager:
    def __init__(self, config: OrchestratorConfig) -> None:
        self._cfg = config
        repo_root = Path(__file__).parent.parent
        self._policies_file = str(repo_root / config.policies_file)
        log.info("Loading policies from %s", self._policies_file)
        self._engine = DLPEngine(self._policies_file)
        # Guards both the swap in _reload_engine and the snapshot read in analyze,
        # so any request whose snapshot is taken AFTER a reload completes is
        # guaranteed to see the new engine (strict hot-reload bar).
        self._engine_lock = threading.Lock()
        # Phase F: track the last-reload wall time so `dlp-ctl status` can report
        # when policies were last (re)loaded.
        self._last_reload_wall = time.time()
        log.info("DLPEngine ready.")

        self._observer = Observer()
        handler = _ReloadHandler(self)
        watch_dir = str(Path(self._policies_file).resolve().parent)
        # Watches analyzer/ (policies.yaml's parent). The Phase B config_watcher
        # watches a different directory (config.yaml's parent), so the two
        # observers are disjoint. _ReloadHandler also filters by filename, which
        # keeps this safe even if the files ever share a directory.
        self._observer.schedule(handler, watch_dir, recursive=False)
        self._observer.daemon = True
        self._observer.start()

    def get_engine(self) -> DLPEngine:
        with self._engine_lock:
            return self._engine

    def last_reload_time(self) -> float:
        """Wall-clock (epoch seconds) of the last successful (re)load."""
        return self._last_reload_wall

    def force_reload(self) -> bool:
        """On-demand reload (dlp-ctl reload / future central-server apply):
        unconditionally rebuild the engine from disk. The file-watcher handles
        automatic apply; this is the authoritative 'apply now'. Returns True on
        success, False if the rebuild failed (the old engine is kept)."""
        return self._reload_engine()

    def _reload_engine(self) -> bool:
        # Construct the new engine OUTSIDE the lock — building a DLPEngine can
        # take 50–200 ms (YAML parse, regex compile, automaton build) and we
        # don't want analyze() calls stalled for that long.
        try:
            new_engine = DLPEngine(self._policies_file)
        except Exception as exc:
            log.error("Policy reload failed, keeping old engine: %s", exc)
            return False
        with self._engine_lock:
            self._engine = new_engine
        self._last_reload_wall = time.time()
        log.info("Policies reloaded from %s", self._policies_file)
        return True

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()

    def _oversize_verdict(self, channel: str, req_id: str, detail: str) -> tuple[str, list]:
        """Verdict for input over the size cap. Follows the channel's unified
        failure_mode (fail_closed → BLOCK default, fail_open → ALLOW); the reason
        is recorded in the log."""
        decision = self._cfg.verdict_for(channel)
        log.warning("reason=size_limit req=%s channel=%s %s -> %s", req_id, channel, detail, decision)
        return decision, []

    def analyze(
        self,
        channel: str,
        kind: str,
        text: str | None = None,
        file_path: str | None = None,
        req_id: str = "",
    ) -> tuple[str, list]:
        with self._engine_lock:
            engine = self._engine  # snapshot — lock release/acquire orders strictly against reload
        if kind == "text":
            body = text or ""
            nbytes = len(body.encode("utf-8", "ignore"))
            cap = getattr(self._cfg, "max_clipboard_bytes", 1 << 60)
            if nbytes > cap:
                return self._oversize_verdict(channel, req_id, f"text bytes={nbytes} > cap={cap}")
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
            cap = getattr(self._cfg, "max_file_bytes", 1 << 60)
            if size > cap:
                verdict = self._oversize_verdict(channel, req_id, f"file={filename} size={size} > cap={cap}")
                try:
                    os.unlink(file_path)
                except OSError as e:
                    log.warning("Could not delete oversized temp file %s: %s", file_path, e)
                return verdict
            content_label = f"file={filename} size={size}"
            cap_chars = self._cfg.max_extracted_chars
            max_chars = cap_chars if cap_chars and cap_chars > 0 else None
            try:
                if is_tabular(file_path):
                    result = engine.analyze_tabular(
                        extract_tabular(file_path, max_chars=max_chars), channel)
                else:
                    result = engine.analyze(
                        extract_text(file_path, max_chars=max_chars), channel)
            except ExtractionTooLarge as exc:
                # Extracted text over analyzer.max_extracted_chars — refuse the
                # analysis and follow the channel's failure_mode (like oversize).
                decision = self._cfg.verdict_for(channel)
                log.warning("reason=text_cap req=%s channel=%s file=%s chars=%d > cap=%s -> %s",
                            req_id, channel, filename, exc.char_count, cap_chars, decision)
                return decision, []
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
    parts = []
    for v in violations:
        s = f"{v.policy_id}({v.action})×{len(v.matches)}"
        cws = getattr(v, "context_words", None)
        if cws:
            s += f"[ctx:{','.join(cws)}]"
        parts.append(s)
    return " ".join(parts)
