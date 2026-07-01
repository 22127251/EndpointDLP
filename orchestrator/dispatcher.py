"""Per-channel ThreadPoolExecutors and clipboard supersession tracker."""
from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any

from orchestrator import messages
from orchestrator.config import OrchestratorConfig
from orchestrator.events import record_decision
from orchestrator.policy_manager import PolicyManager

log = logging.getLogger(__name__)

# Default analysis budget (seconds) when config doesn't specify one. The real
# value comes from cfg.analysis_timeout_seconds (config.yaml service:). INVARIANT:
# every client's pipe timeout must EXCEED this, or the client gives up before the
# orchestrator answers (config.yaml ships analysis=10 s, client waits=12 s).
_ANALYSIS_TIMEOUT = 4.0

_CHANNELS = ("clipboard", "browser", "peripheral_storage")


class Dispatcher:
    def __init__(self, cfg: OrchestratorConfig, policy_manager: PolicyManager) -> None:
        self._cfg = cfg
        self._pm = policy_manager
        self._clipboard_pool = ThreadPoolExecutor(
            max_workers=cfg.clipboard_workers, thread_name_prefix="dlp-clip"
        )
        self._browser_pool = ThreadPoolExecutor(
            max_workers=cfg.browser_workers, thread_name_prefix="dlp-browser"
        )
        self._peripheral_pool = ThreadPoolExecutor(
            max_workers=cfg.peripheral_storage_workers, thread_name_prefix="dlp-periph"
        )

        self._clip_seq: int = 0
        self._clip_lock = threading.Lock()
        self._clip_inflight: dict[int, threading.Event] = {}  # seq → cancel flag

        # Phase F: in-flight bookkeeping for `dlp-ctl status` and the stop drain.
        self._active: set[Future] = set()
        self._active_lock = threading.Lock()
        self._inflight_counts: dict[str, int] = {ch: 0 for ch in _CHANNELS}

        # Cloud bridge: optional violation callback (set after CloudBridge init)
        self._violation_callback: Any = None

    @property
    def _analysis_timeout(self) -> float:
        """Live analysis budget (seconds), read from the hot-reloadable config on
        every access so a `service.analysis_timeout_seconds` change applies to the
        next analysis without a restart."""
        return getattr(self._cfg, "analysis_timeout_seconds", _ANALYSIS_TIMEOUT)

    def analyze(self, request: dict) -> tuple[str, bool, str]:
        """
        Run analysis for *request* in the appropriate thread pool.

        Blocks the calling thread until analysis completes (or times out).

        Returns (decision, write_response, reason):
          - decision: "ALLOW" or "BLOCK"
          - write_response: False if this clipboard request was superseded and
            the response should be silently dropped.
          - reason: end-user block message (empty string if ALLOW). Sent to ALL
            channels now (clipboard, browser, peripheral_storage); the server
            wraps it as "BLOCK|reason".

        Each per-channel helper returns (decision, violations, category) where
        category is the machine token behind the verdict (None on a clean ALLOW).
        The user-facing reason is derived uniformly from that triple, and the
        category is what the audit log records as `reason`.
        """
        channel = request.get("channel", "browser")
        req_id = request.get("req_id", "?")
        t0 = time.perf_counter()
        if channel == "clipboard":
            decision, write_response, violations, category = self._analyze_clipboard(request)
        elif channel == "peripheral_storage":
            decision, violations, category = self._analyze_peripheral(request)
            write_response = True
        else:
            decision, violations, category = self._analyze_browser(request)
            write_response = True
        reason = _user_reason(decision, violations, category)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._emit_event(
            request, channel, decision, violations, elapsed_ms, req_id,
            superseded=(channel == "clipboard" and not write_response),
            reason=category,
        )
        return decision, write_response, reason

    def inflight_counts(self) -> dict[str, int]:
        """Snapshot of per-channel in-flight analysis counts (for dlp-ctl status)."""
        with self._active_lock:
            return dict(self._inflight_counts)

    def drain(self, timeout: float) -> int:
        """Wait up to *timeout* s for in-flight analyses, then cancel the rest.

        Returns the number of analyses still running at the deadline (abandoned).
        ThreadPoolExecutor.shutdown has no timeout, so we wait on the active
        futures with a deadline then shutdown(wait=False, cancel_futures=True).
        """
        with self._active_lock:
            pending = list(self._active)
        not_done: set[Future] = set()
        if pending:
            _, not_done = concurrent.futures.wait(pending, timeout=timeout)
        for pool in (self._clipboard_pool, self._browser_pool, self._peripheral_pool):
            pool.shutdown(wait=False, cancel_futures=True)
        return len(not_done)

    def shutdown(self, wait: bool = True) -> None:
        self._clipboard_pool.shutdown(wait=wait)
        self._browser_pool.shutdown(wait=wait)
        self._peripheral_pool.shutdown(wait=wait)

    # ------------------------------------------------------------------ #

    def _tracked_submit(self, channel: str, pool: ThreadPoolExecutor, *args, **kwargs) -> Future:
        """Submit pm.analyze(*args, **kwargs) and register the future for
        status/drain bookkeeping (count decremented when the future completes)."""
        future = pool.submit(self._pm.analyze, *args, **kwargs)
        with self._active_lock:
            self._active.add(future)
            self._inflight_counts[channel] += 1

        def _done(f: Future, ch: str = channel) -> None:
            with self._active_lock:
                self._active.discard(f)
                self._inflight_counts[ch] -= 1

        future.add_done_callback(_done)
        return future

    def set_violation_callback(self, callback: Any) -> None:
        """Set callback invoked with violation dicts when BLOCK decisions occur."""
        self._violation_callback = callback

    def _emit_event(
        self, request: dict, channel: str, decision: str, violations: list,
        elapsed_ms: float, req_id: str, *, superseded: bool,
        reason: str | None = None,
    ) -> None:
        meta = request.get("metadata") or {}
        name = meta.get("filename") or os.path.basename(request.get("file_path") or "") or None
        url = meta.get("url") or None

        # Build the per-policy match list ONCE and feed BOTH events.jsonl and the cloud
        # bridge, so the server's violation log is a faithful mirror of the audit log.
        # context_words_triggered = the distinct words that boosted a match (NOT the
        # policy's full context list).
        matches = [
            {"policy_id": getattr(v, "policy_id", None),
             "action": getattr(v, "action", ""),
             "count": len(getattr(v, "matches", []) or []),
             "with_context": sum(
                 1 for m in (getattr(v, "matches", []) or [])
                 if getattr(m, "has_context", False)
             ),
             "context_words_triggered": list(getattr(v, "context_words", []) or [])}
            for v in violations
        ]
        try:
            record_decision(
                channel=channel,
                kind=request.get("kind", ""),
                name=name,
                url=url,
                decision=decision,
                violations=matches,
                elapsed_ms=elapsed_ms,
                req_id=req_id,
                superseded=superseded,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — audit logging must never break a decision
            log.warning("event log failed for req=%s: %s", req_id, exc)

        # Cloud bridge: report any NOTABLE decision — a policy match OR a failure
        # `reason` (so fail_open allows and fail_closed blocks are reported too, not
        # just policy blocks). A clean ALLOW (no match, no reason) is not sent.
        # agent_id is filled by CloudBridge.
        if self._violation_callback is not None and (matches or reason):
            try:
                self._violation_callback({
                    "channel": channel,
                    "decision": decision,
                    "reason": reason,
                    "details": {
                        "req_id": req_id,
                        "name": name or "",
                        "url": url or "",
                        "elapsed_ms": round(elapsed_ms, 1),
                    },
                    "matches": matches,
                })
            except Exception:
                log.debug("violation callback failed", exc_info=True)

    def _analyze_browser(self, request: dict) -> tuple[str, list, str | None]:
        req_id = request.get("req_id", "?")
        future = self._tracked_submit(
            "browser", self._browser_pool,
            request["channel"],
            request["kind"],
            text=request.get("text"),
            file_path=request.get("file_path"),
            req_id=req_id,
        )
        try:
            decision, violations, failure = future.result(timeout=self._analysis_timeout)
            return decision, violations, _category(decision, violations, failure)
        except FutureTimeoutError:
            verdict = self._cfg.verdict_for("browser")
            log.error("reason=timeout req=%s channel=browser after %.1fs; failing %s",
                      req_id, self._analysis_timeout, _fail_word(verdict))
            future.cancel()
            return verdict, [], _category(verdict, [], "timeout")
        except Exception as exc:
            verdict = self._cfg.verdict_for("browser")
            log.error("reason=error req=%s channel=browser: %s; failing %s",
                      req_id, exc, _fail_word(verdict))
            return verdict, [], _category(verdict, [], "analysis_error")

    def _analyze_peripheral(self, request: dict) -> tuple[str, list, str | None]:
        req_id = request.get("req_id", "?")
        future = self._tracked_submit(
            "peripheral_storage", self._peripheral_pool,
            request["channel"],
            request["kind"],
            text=request.get("text"),
            file_path=request.get("file_path"),
            req_id=req_id,
        )
        try:
            decision, violations, failure = future.result(timeout=self._analysis_timeout)
            return decision, violations, _category(decision, violations, failure)
        except FutureTimeoutError:
            verdict = self._cfg.verdict_for("peripheral_storage")
            log.error("reason=timeout req=%s channel=peripheral_storage after %.1fs; failing %s",
                      req_id, self._analysis_timeout, _fail_word(verdict))
            future.cancel()
            return verdict, [], _category(verdict, [], "timeout")
        except Exception as exc:
            verdict = self._cfg.verdict_for("peripheral_storage")
            log.error("reason=error req=%s channel=peripheral_storage: %s; failing %s",
                      req_id, exc, _fail_word(verdict))
            return verdict, [], _category(verdict, [], "analysis_error")

    def _analyze_clipboard(self, request: dict) -> tuple[str, bool, list, str | None]:
        req_id = request.get("req_id", "?")
        with self._clip_lock:
            seq = self._clip_seq + 1
            self._clip_seq = seq
            for old_seq, flag in list(self._clip_inflight.items()):
                if old_seq < seq:
                    flag.set()
            cancel_flag = threading.Event()
            self._clip_inflight[seq] = cancel_flag

        violations: list = []
        category: str | None = None
        try:
            future = self._tracked_submit(
                "clipboard", self._clipboard_pool,
                request["channel"],
                request["kind"],
                text=request.get("text"),
                file_path=request.get("file_path"),
                req_id=req_id,
            )
            try:
                decision, violations, failure = future.result(timeout=self._analysis_timeout)
                category = _category(decision, violations, failure)
            except FutureTimeoutError:
                decision = self._cfg.verdict_for("clipboard")
                category = _category(decision, [], "timeout")
                log.error("reason=timeout req=%s clip_seq=%d; failing %s",
                          req_id, seq, _fail_word(decision))
                future.cancel()
            except Exception as exc:
                decision = self._cfg.verdict_for("clipboard")
                category = _category(decision, [], "analysis_error")
                log.error("reason=error req=%s clip_seq=%d: %s; failing %s",
                          req_id, seq, exc, _fail_word(decision))
        finally:
            with self._clip_lock:
                self._clip_inflight.pop(seq, None)

        if cancel_flag.is_set():
            log.info("superseded req=%s clip_seq=%d by seq=%d decision=%s",
                     req_id, seq, self._clip_seq, decision)
            return decision, False, violations, category

        return decision, True, violations, category


def _fail_word(verdict: str) -> str:
    """Log token for the failure_mode that produced *verdict* (BLOCK→closed,
    ALLOW→open), so failure logs read 'failing closed' / 'failing open'."""
    return "closed" if verdict == "BLOCK" else "open"


def _category(decision: str, violations: list, failure: str | None) -> str | None:
    """The machine token behind an outcome (logged to events.jsonl as `reason`).
    A refused analysis carries an explicit *failure* token (oversize / text_cap /
    unsupported_format / timeout / analysis_error / malformed); a completed BLOCK
    with violations is a real policy hit (`policy_violation`); a clean ALLOW has
    no category (None)."""
    if failure:
        return failure
    if decision == "BLOCK":
        return "policy_violation"
    return None


def _user_reason(decision: str, violations: list, category: str | None) -> str:
    """End-user block message for a decision. Empty on ALLOW. A policy hit uses
    the matched policies' admin-editable user_message (via _format_block_reason);
    a failure category uses the per-category message table (orchestrator.messages).
    The policy id is NEVER shown."""
    if decision != "BLOCK":
        return ""
    if category and category != "policy_violation":
        return messages.failure_message(category)
    return _format_block_reason(violations)


def _format_block_reason(violations: list) -> str:
    """End-user reason for a policy block, from the matched policies'
    ``user_message`` (admin-editable, in policies.yaml). Distinct messages are
    joined; policies with no user_message contribute the generic fallback. Never
    derives text from the policy id (insecure / unstable)."""
    seen: list[str] = []
    for v in violations:
        msg = (getattr(v, "user_message", "") or "").strip() or messages.GENERIC_POLICY_MESSAGE
        if msg not in seen:
            seen.append(msg)
    if not seen:
        return messages.GENERIC_POLICY_MESSAGE
    return "; ".join(seen)
