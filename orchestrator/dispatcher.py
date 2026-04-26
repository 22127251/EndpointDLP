"""Per-channel ThreadPoolExecutors and clipboard supersession tracker."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from orchestrator.config import OrchestratorConfig
from orchestrator.policy_manager import PolicyManager

log = logging.getLogger(__name__)

# How long an accept thread waits for analysis before giving up (seconds).
# Must be less than the client's pipe timeout (5 s in pipe_client.py).
_ANALYSIS_TIMEOUT = 4.0


class Dispatcher:
    def __init__(self, cfg: OrchestratorConfig, policy_manager: PolicyManager) -> None:
        self._pm = policy_manager
        self._clipboard_pool = ThreadPoolExecutor(
            max_workers=cfg.clipboard_workers, thread_name_prefix="dlp-clip"
        )
        self._browser_pool = ThreadPoolExecutor(
            max_workers=cfg.browser_workers, thread_name_prefix="dlp-browser"
        )

        self._clip_seq: int = 0
        self._clip_lock = threading.Lock()
        self._clip_inflight: dict[int, threading.Event] = {}  # seq → cancel flag

    def analyze(self, request: dict) -> tuple[str, bool]:
        """
        Run analysis for *request* in the appropriate thread pool.

        Blocks the calling thread until analysis completes (or times out).

        Returns (decision, write_response):
          - decision: "ALLOW" or "BLOCK"
          - write_response: False if this clipboard request was superseded and
            the response should be silently dropped.
        """
        channel = request.get("channel", "browser")
        if channel == "clipboard":
            return self._analyze_clipboard(request)
        return self._analyze_browser(request), True

    def shutdown(self, wait: bool = True) -> None:
        self._clipboard_pool.shutdown(wait=wait)
        self._browser_pool.shutdown(wait=wait)

    # ------------------------------------------------------------------ #

    def _analyze_browser(self, request: dict) -> str:
        req_id = request.get("req_id", "?")
        future = self._browser_pool.submit(
            self._pm.analyze,
            request["channel"],
            request["kind"],
            text=request.get("text"),
            file_path=request.get("file_path"),
            req_id=req_id,
        )
        try:
            decision, _violations = future.result(timeout=_ANALYSIS_TIMEOUT)
            return decision
        except FutureTimeoutError:
            log.error("timeout req=%s channel=browser after %.1fs; failing closed",
                      req_id, _ANALYSIS_TIMEOUT)
            future.cancel()
            return "BLOCK"
        except Exception as exc:
            log.error("error req=%s channel=browser: %s", req_id, exc)
            return "BLOCK"

    def _analyze_clipboard(self, request: dict) -> tuple[str, bool]:
        req_id = request.get("req_id", "?")
        with self._clip_lock:
            seq = self._clip_seq + 1
            self._clip_seq = seq
            for old_seq, flag in list(self._clip_inflight.items()):
                if old_seq < seq:
                    flag.set()
            cancel_flag = threading.Event()
            self._clip_inflight[seq] = cancel_flag

        try:
            future = self._clipboard_pool.submit(
                self._pm.analyze,
                request["channel"],
                request["kind"],
                text=request.get("text"),
                file_path=request.get("file_path"),
                req_id=req_id,
            )
            try:
                decision, _violations = future.result(timeout=_ANALYSIS_TIMEOUT)
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
            return decision, False

        return decision, True
