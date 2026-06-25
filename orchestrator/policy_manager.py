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

    def _oversize_verdict(self, channel: str, req_id: str, detail: str) -> tuple[str, list, str]:
        """Verdict for input over the size cap. Follows the channel's unified
        failure_mode (fail_closed → BLOCK default, fail_open → ALLOW); the reason
        is recorded in the log. Third tuple element is the failure category the
        dispatcher maps to a user message + the events.jsonl `reason` token."""
        decision = self._cfg.verdict_for(channel)
        log.warning("reason=size_limit req=%s channel=%s %s -> %s", req_id, channel, detail, decision)
        return decision, [], "oversize"

    def analyze(
        self,
        channel: str,
        kind: str,
        text: str | None = None,
        file_path: str | None = None,
        req_id: str = "",
    ) -> tuple[str, list, str | None]:
        """Return (decision, violations, failure_category). failure_category is a
        token (oversize / text_cap / unsupported_format / malformed) when the
        analysis was refused, or None for a completed analysis (allow / policy
        block). The dispatcher turns the token into the user message + the
        events.jsonl `reason`."""
        with self._engine_lock:
            engine = self._engine  # snapshot — lock release/acquire orders strictly against reload
        if kind == "text":
            body = text or ""
            # Clipboard text has no file to extract, so the analyzer's
            # max_extracted_chars governs whether it is scanned (parity with the
            # file-extraction cap). Over the cap → refuse without scanning
            # (reason=text_cap) and follow the channel's failure_mode. <=0 disables
            # the cap (scan everything the pipe accepts; see server.py ceiling).
            cap_chars = self._cfg.max_extracted_chars
            if cap_chars and cap_chars > 0 and len(body) > cap_chars:
                decision = self._cfg.verdict_for(channel)
                log.warning("reason=text_cap req=%s channel=%s text chars=%d > cap=%d -> %s",
                            req_id, channel, len(body), cap_chars, decision)
                return decision, [], "text_cap"
            result = engine.analyze(body, channel)
            content_label = (
                f"size={len(body)} hash={hashlib.sha256(body.encode()).hexdigest()[:8]}"
            )
        elif kind == "file":
            if not file_path:
                log.error("kind=file but no file_path provided; failing closed.")
                return "BLOCK", [], "malformed"
            filename = os.path.basename(file_path)
            # Supported-format gate: refuse an extension the analyzer was not
            # built/tested to extract (e.g. .exe, .jpg, .pptx) BEFORE extraction,
            # so it is never scanned as garbage text. Follows the channel's
            # failure_mode (reason=unsupported_format), like the oversize path.
            #
            # An EMPTY extension is NOT refused: some upload paths strip it (Gmail
            # delivers every file as "upload" with no extension), so blocking on a
            # missing extension would block legitimate .txt/.csv/.md uploads. Those
            # fall through to the analyzer's plaintext path — PII recall is
            # preserved (a binary blob with no extension simply yields no matches).
            # Only an explicit, non-empty, unsupported extension is refused.
            ext = os.path.splitext(filename)[1].lower()
            if ext and ext not in self._cfg.supported_extensions:
                decision = self._cfg.verdict_for(channel)
                log.warning("reason=unsupported_format req=%s channel=%s file=%s ext=%s -> %s",
                            req_id, channel, filename, ext or "(none)", decision)
                try:
                    os.unlink(file_path)
                except OSError as e:
                    log.warning("Could not delete unsupported temp file %s: %s", file_path, e)
                return decision, [], "unsupported_format"
            size = os.path.getsize(file_path)
            cap = getattr(self._cfg, "max_file_bytes", 1 << 60)
            if size > cap:
                verdict = self._oversize_verdict(channel, req_id, f"file={filename} size={size} > cap={cap}")
                try:
                    os.unlink(file_path)
                except OSError as e:
                    log.warning("Could not delete oversized temp file %s: %s", file_path, e)
                return verdict
            # DIAG (dlp-agent.log only): fingerprint the EXACT bytes about to be
            # analyzed. A "same file → different counts" report can then be pinned
            # to the INPUT (a differing sha8 = the client delivered different bytes)
            # vs the analyzer (identical sha8 but different counts). Reversible.
            sha8 = _sha8_of_file(file_path)
            cap_chars = self._cfg.max_extracted_chars
            max_chars = cap_chars if cap_chars and cap_chars > 0 else None
            try:
                if is_tabular(file_path):
                    td = extract_tabular(file_path, max_chars=max_chars)
                    extracted_chars = _tabular_char_count(td)
                    result = engine.analyze_tabular(td, channel)
                else:
                    text = extract_text(file_path, max_chars=max_chars)
                    extracted_chars = len(text)
                    result = engine.analyze(text, channel)
            except ExtractionTooLarge as exc:
                # Extracted text over analyzer.max_extracted_chars — refuse the
                # analysis and follow the channel's failure_mode (like oversize).
                decision = self._cfg.verdict_for(channel)
                log.warning("reason=text_cap req=%s channel=%s file=%s size=%d sha8=%s chars=%d > cap=%s -> %s",
                            req_id, channel, filename, size, sha8, exc.char_count, cap_chars, decision)
                return decision, [], "text_cap"
            finally:
                try:
                    os.unlink(file_path)
                except OSError as e:
                    log.warning("Could not delete temp file %s: %s", file_path, e)
            content_label = f"file={filename} size={size} sha8={sha8} extracted_chars={extracted_chars}"
            # One INFO line per file decision carrying the input fingerprint + result,
            # so even an ALLOW (which logs at DEBUG below) is captured for correlation.
            log.info("DIAG req=%s channel=%s file=%s size=%d sha8=%s extracted_chars=%d action=%s counts=[%s]",
                     req_id, channel, filename, size, sha8, extracted_chars,
                     result.applied_action, _fmt_violations(result.violations))
        else:
            log.error("Unknown kind=%r; failing closed.", kind)
            return "BLOCK", [], "malformed"

        action = result.applied_action
        if action == "block":
            log.warning("BLOCK req=%s channel=%s %s elapsed=%.1fms violations=[%s]",
                        req_id, channel, content_label, result.elapsed_ms,
                        _fmt_violations(result.violations))
            return "BLOCK", result.violations, None
        elif action == "allow_log":
            log.info("ALLOW(logged) req=%s channel=%s %s elapsed=%.1fms violations=[%s]",
                     req_id, channel, content_label, result.elapsed_ms,
                     _fmt_violations(result.violations))
            return "ALLOW", result.violations, None
        else:
            log.debug("ALLOW req=%s channel=%s %s elapsed=%.1fms",
                      req_id, channel, content_label, result.elapsed_ms)
            return "ALLOW", result.violations, None


def _sha8_of_file(path: str) -> str:
    """First 8 hex chars of the SHA-256 of *path*'s bytes — a cheap fingerprint
    of the exact input the analyzer saw (streamed in 1 MB chunks). Returns
    '????????' if the file can't be read. DIAGNOSTIC only (dlp-agent.log)."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return "????????"
    return h.hexdigest()[:8]


def _tabular_char_count(td) -> int:
    """Extracted-character count for tabular data (cell values + body; headers
    negligible) — mirrors extractor._enforce_tabular_cap so the logged
    extracted_chars matches the cap accounting."""
    return (sum(len(v) for c in td.columns for v in c.values)
            + sum(len(b) for b in td.body))


def _fmt_violations(violations: list) -> str:
    parts = []
    for v in violations:
        s = f"{v.policy_id}({v.action})×{len(v.matches)}"
        cws = getattr(v, "context_words", None)
        if cws:
            s += f"[ctx:{','.join(cws)}]"
        parts.append(s)
    return " ".join(parts)
