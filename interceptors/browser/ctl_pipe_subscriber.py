"""Python ctl-pipe subscriber for the mitmproxy addon.

Mirrors src/DlpShared/CtlPipeSubscriber.cs:
- Connects to the orchestrator's ctl-pipe in message mode.
- Sends a single `subscribe` message (with snapshot_request=true).
- Dispatches config_snapshot / config_update payloads to on_change.
- Reconnects on transient errors with exponential backoff (250 ms → 4 s).
- Backs off and retries on `already_subscribed`; exits on other error codes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable

import pywintypes
import win32event
import win32file
import win32pipe

log = logging.getLogger(__name__)

_ERROR_PIPE_BUSY = 231
_ERROR_IO_PENDING = 997
_ERROR_BROKEN_PIPE = 109
_BACKOFF_INITIAL_S = 0.25
_BACKOFF_MAX_S = 4.0
_CONNECT_TIMEOUT_MS = 5000


class CtlPipeSubscriber:
    """Daemon-thread ctl-pipe subscriber.

    Usage:
        sub = CtlPipeSubscriber(pipe_name, "browser", on_change)
        sub.start()
        ...
        sub.stop()   # idempotent
    """

    def __init__(
        self,
        pipe_name: str,
        component_name: str,
        on_change: Callable[[dict], None],
    ) -> None:
        self._pipe_name = pipe_name
        self._component_name = component_name
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"ctl-sub-{self._component_name}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- internals ---------------------------------------------------------

    def _run(self) -> None:
        backoff = _BACKOFF_INITIAL_S
        while not self._stop.is_set():
            try:
                self._run_once()
                backoff = _BACKOFF_INITIAL_S  # clean disconnect resets backoff
            except _AlreadySubscribed as exc:
                log.warning(
                    "ctl: already_subscribed (component=%s); backing off — %s",
                    self._component_name, exc,
                )
                self._sleep_with_stop(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
            except _FatalError as exc:
                log.error(
                    "ctl: fatal error code=%s — exiting subscriber. %s",
                    exc.code, exc,
                )
                return
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "ctl: subscriber error: %s; reconnecting in %.0fms",
                    exc, backoff * 1000,
                )
                self._sleep_with_stop(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)

    def _run_once(self) -> None:
        handle = self._open()
        try:
            win32pipe.SetNamedPipeHandleState(
                handle, win32pipe.PIPE_READMODE_MESSAGE, None, None,
            )
            self._send_subscribe(handle)
            log.info("ctl: subscribed component=%s", self._component_name)
            while not self._stop.is_set():
                msg = self._read_message(handle)
                if msg is None:
                    raise OSError("ctl pipe closed by server")
                self._dispatch(msg)
        finally:
            try:
                win32file.CloseHandle(handle)
            except pywintypes.error:
                pass

    def _open(self):
        deadline = time.monotonic() + _CONNECT_TIMEOUT_MS / 1000.0
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"could not open ctl pipe {self._pipe_name}")
            try:
                win32pipe.WaitNamedPipe(self._pipe_name, max(1, int(min(500, remaining * 1000))))
            except pywintypes.error:
                time.sleep(0.05)
                continue
            try:
                return win32file.CreateFile(
                    self._pipe_name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None,
                    win32file.OPEN_EXISTING,
                    win32file.FILE_FLAG_OVERLAPPED,
                    None,
                )
            except pywintypes.error as exc:
                if exc.winerror == _ERROR_PIPE_BUSY:
                    continue
                raise
        raise InterruptedError("stop signalled before connect")

    def _send_subscribe(self, handle) -> None:
        msg = {
            "type": "subscribe",
            "component": self._component_name,
            "pid": os.getpid(),
            "snapshot_request": True,
        }
        data = json.dumps(msg).encode("utf-8")
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            try:
                win32file.WriteFile(handle, data, overlapped)
            except pywintypes.error as exc:
                if exc.winerror != _ERROR_IO_PENDING:
                    raise
            rc = win32event.WaitForSingleObject(overlapped.hEvent, _CONNECT_TIMEOUT_MS)
            if rc != win32event.WAIT_OBJECT_0:
                try:
                    win32file.CancelIo(handle)
                except pywintypes.error:
                    pass
                raise TimeoutError("ctl subscribe write timed out")
            win32file.GetOverlappedResult(handle, overlapped, False)
        finally:
            win32file.CloseHandle(overlapped.hEvent)

    def _read_message(self, handle) -> dict | None:
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            buf = win32file.AllocateReadBuffer(64 * 1024)
            try:
                win32file.ReadFile(handle, buf, overlapped)
            except pywintypes.error as exc:
                if exc.winerror == _ERROR_BROKEN_PIPE:
                    return None
                if exc.winerror != _ERROR_IO_PENDING:
                    raise
            while not self._stop.is_set():
                rc = win32event.WaitForSingleObject(overlapped.hEvent, 200)
                if rc == win32event.WAIT_OBJECT_0:
                    try:
                        n = win32file.GetOverlappedResult(handle, overlapped, False)
                    except pywintypes.error as exc:
                        if exc.winerror == _ERROR_BROKEN_PIPE:
                            return None
                        raise
                    if n == 0:
                        return None
                    return json.loads(bytes(buf[:n]).decode("utf-8"))
            try:
                win32file.CancelIo(handle)
            except pywintypes.error:
                pass
            return None
        finally:
            win32file.CloseHandle(overlapped.hEvent)

    def _dispatch(self, msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type in ("config_snapshot", "config_update"):
            version = msg.get("version", 0)
            log.info("ctl: %s received (version=%s)", msg_type, version)
            try:
                self._on_change(msg.get("config") or {})
            except Exception:
                log.exception("ctl: on_change callback failed")
        elif msg_type == "error":
            code = msg.get("code", "")
            message = msg.get("message", "")
            if code == "already_subscribed":
                raise _AlreadySubscribed(message)
            raise _FatalError(code, message)
        else:
            log.warning("ctl: ignoring unknown message type=%s", msg_type)

    def _sleep_with_stop(self, seconds: float) -> None:
        # Cooperative sleep — respects stop() requests.
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.1))


class _AlreadySubscribed(Exception):
    pass


class _FatalError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
