"""Pipe-client helper for harness tests.

Mirrors interceptors.browser.pipe_client.send_and_receive semantics
(overlapped I/O + bounded deadline) but lives separately so harness changes
don't churn the production client.
"""
from __future__ import annotations

import json
import time

import pywintypes
import win32con
import win32event
import win32file
import win32pipe

_ERROR_IO_PENDING = 997
_ERROR_PIPE_BUSY = 231


def pipe_send(
    pipe_name: str, payload: dict, timeout_seconds: float
) -> tuple[str, str]:
    """Send JSON, read decision. Returns (decision, reason).

    Raises TimeoutError if the exchange exceeds timeout_seconds.
    Raises OSError / pywintypes.error on pipe errors (e.g. broken pipe).
    """
    deadline = time.monotonic() + timeout_seconds
    handle = _connect_with_retry(pipe_name, deadline)
    try:
        win32pipe.SetNamedPipeHandleState(
            handle, win32pipe.PIPE_READMODE_MESSAGE, None, None
        )
        _async_write(handle, json.dumps(payload).encode("utf-8"), deadline)
        response_bytes = _async_read(handle, 64 * 1024, deadline)
        response = response_bytes.decode("utf-8").strip()
        if response.startswith("BLOCK"):
            reason = response.split("|", 1)[1] if "|" in response else ""
            return "BLOCK", reason
        if response.upper() == "ALLOW":
            return "ALLOW", ""
        raise ValueError(f"Unexpected pipe response: {response!r}")
    finally:
        win32file.CloseHandle(handle)


def _connect_with_retry(pipe_name: str, deadline: float):
    """WaitNamedPipe + CreateFile, retrying on ERROR_PIPE_BUSY until deadline."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"could not open pipe '{pipe_name}' before deadline")
        wait_ms = max(1, min(500, int(remaining * 1000)))
        try:
            win32pipe.WaitNamedPipe(pipe_name, wait_ms)
        except pywintypes.error:
            # 121 ERROR_SEM_TIMEOUT (no instance) or 2 ERROR_FILE_NOT_FOUND.
            # Either way, retry until the deadline.
            continue
        try:
            return win32file.CreateFile(
                pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING,
                win32con.FILE_FLAG_OVERLAPPED,
                None,
            )
        except pywintypes.error as exc:
            if exc.winerror == _ERROR_PIPE_BUSY:
                continue   # another client grabbed the slot first
            raise


def _async_write(handle, data: bytes, deadline: float) -> None:
    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
    try:
        try:
            win32file.WriteFile(handle, data, overlapped)
        except pywintypes.error as exc:
            if exc.winerror != _ERROR_IO_PENDING:
                raise
        _wait_or_cancel(handle, overlapped, deadline, op="write")
        win32file.GetOverlappedResult(handle, overlapped, False)
    finally:
        win32file.CloseHandle(overlapped.hEvent)


def _async_read(handle, max_bytes: int, deadline: float) -> bytes:
    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
    try:
        buf = win32file.AllocateReadBuffer(max_bytes)
        try:
            win32file.ReadFile(handle, buf, overlapped)
        except pywintypes.error as exc:
            if exc.winerror != _ERROR_IO_PENDING:
                raise
        _wait_or_cancel(handle, overlapped, deadline, op="read")
        n = win32file.GetOverlappedResult(handle, overlapped, False)
        return bytes(buf[:n])
    finally:
        win32file.CloseHandle(overlapped.hEvent)


def _wait_or_cancel(handle, overlapped, deadline: float, op: str) -> None:
    remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
    rc = win32event.WaitForSingleObject(overlapped.hEvent, remaining_ms)
    if rc == win32event.WAIT_OBJECT_0:
        return
    if rc == win32event.WAIT_TIMEOUT:
        try:
            win32file.CancelIo(handle)
        except pywintypes.error:
            pass
        raise TimeoutError(f"pipe {op} timed out")
    raise OSError(f"WaitForSingleObject returned unexpected status: {rc}")
