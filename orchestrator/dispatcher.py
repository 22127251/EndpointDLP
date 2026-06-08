"""Per-channel ThreadPoolExecutors and clipboard supersession tracker."""
from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from orchestrator.config import OrchestratorConfig
from orchestrator.events import record_decision
from orchestrator.policy_manager import PolicyManager

log = logging.getLogger(__name__)

# How long an accept thread waits for analysis before giving up (seconds).
# Must be less than the client's pipe timeout (5 s in pipe_client.py).
_ANALYSIS_TIMEOUT = 4.0

_CHANNELS = ("clipboard", "browser", "peripheral_storage")


class Dispatcher:
    def __init__(self, cfg: OrchestratorConfig, policy_manager: PolicyManager) -> None:
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

    def analyze(self, request: dict) -> tuple[str, bool, str]:
        """
        Run analysis for *request* in the appropriate thread pool.

        Blocks the calling thread until analysis completes (or times out).

        Returns (decision, write_response, reason):
          - decision: "ALLOW" or "BLOCK"
          - write_response: False if this clipboard request was superseded and
            the response should be silently dropped.
          - reason: human-readable reason for the decision (empty string if ALLOW)
        """
        channel = request.get("channel", "browser")
        req_id = request.get("req_id", "?")
        t0 = time.perf_counter()
        if channel == "clipboard":
            decision, write_response, violations = self._analyze_clipboard(request)
            reason = ""
        elif channel == "peripheral_storage":
            decision, violations = self._analyze_peripheral(request)
            write_response, reason = True, ""
        else:
            decision, reason, violations = self._analyze_browser(request)
            write_response = True
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._emit_event(
            request, channel, decision, violations, elapsed_ms, req_id,
            superseded=(channel == "clipboard" and not write_response),
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

    def _emit_event(
        self, request: dict, channel: str, decision: str, violations: list,
        elapsed_ms: float, req_id: str, *, superseded: bool,
    ) -> None:
        meta = request.get("metadata") or {}
        name = meta.get("filename") or os.path.basename(request.get("file_path") or "") or None
        url = meta.get("url") or None
        try:
            record_decision(
                channel=channel,
                kind=request.get("kind", ""),
                name=name,
                url=url,
                decision=decision,
                violations=[
                    {"policy_id": getattr(v, "policy_id", "?"),
                     "count": len(getattr(v, "matches", []) or [])}
                    for v in violations
                ],
                elapsed_ms=elapsed_ms,
                req_id=req_id,
                superseded=superseded,
            )
        except Exception as exc:  # noqa: BLE001 — audit logging must never break a decision
            log.warning("event log failed for req=%s: %s", req_id, exc)

    def _analyze_browser(self, request: dict) -> tuple[str, str, list]:
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
            decision, violations = future.result(timeout=_ANALYSIS_TIMEOUT)
            reason = _format_block_reason(violations) if decision == "BLOCK" else ""
            return decision, reason, violations
        except FutureTimeoutError:
            log.error("timeout req=%s channel=browser after %.1fs; failing closed",
                      req_id, _ANALYSIS_TIMEOUT)
            future.cancel()
            return "BLOCK", "Analysis timed out", []
        except Exception as exc:
            log.error("error req=%s channel=browser: %s", req_id, exc)
            return "BLOCK", "Analysis error", []

    def _analyze_peripheral(self, request: dict) -> tuple[str, list]:
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
            decision, violations = future.result(timeout=_ANALYSIS_TIMEOUT)
            return decision, violations
        except FutureTimeoutError:
            log.error("timeout req=%s channel=peripheral_storage after %.1fs; failing closed",
                      req_id, _ANALYSIS_TIMEOUT)
            future.cancel()
            return "BLOCK", []
        except Exception as exc:
            log.error("error req=%s channel=peripheral_storage: %s", req_id, exc)
            return "BLOCK", []

    def _analyze_clipboard(self, request: dict) -> tuple[str, bool, list]:
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
                decision, violations = future.result(timeout=_ANALYSIS_TIMEOUT)
            except FutureTimeoutError:
                log.error("timeout req=%s clip_seq=%d; failing closed", req_id, seq)
                future.cancel()
                decision = "BLOCK"
            except Exception as exc:
                log.error("error req=%s clip_seq=%d: %s", req_id, seq, exc)
                decision = "BLOCK"
        finally:
            with self._clip_lock:
                self._clip_inflight.pop(seq, None)

        if cancel_flag.is_set():
            log.info("superseded req=%s clip_seq=%d by seq=%d decision=%s",
                     req_id, seq, self._clip_seq, decision)
            return decision, False, violations

        return decision, True, violations


def _format_block_reason(violations: list) -> str:
    """Format a human-readable block reason from violation policy IDs."""
    if not violations:
        return "Sensitive data detected"
    names = []
    for v in violations:
        # Turn "block_visa_browser" into "Visa Card"
        name = v.policy_id.replace("block_", "").replace("_browser", "").replace("_", " ").title()
        names.append(name)
    return "Sensitive data detected: " + ", ".join(names)
