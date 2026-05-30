"""
Windows named pipe client.

Sends a JSON payload to the pipe server and reads back "ALLOW" or "BLOCK".
Optionally returns a reason string when the decision is "BLOCK".
Raises TimeoutError if no response within timeout_seconds.
Raises OSError if the pipe cannot be opened (server not running).
"""

import json
import time

import pywintypes
import win32con
import win32event
import win32file
import win32pipe


_ERROR_IO_PENDING = 997
_ERROR_PIPE_BUSY = 231


def send_and_receive(payload: dict, pipe_name: str, timeout_seconds: float) -> tuple[str, str]:
    """
    Open the named pipe, send JSON, read response, return (decision, reason).
    decision is "ALLOW" or "BLOCK". reason is non-empty only on BLOCK.

    The whole exchange (wait-for-pipe + write + read) is bounded by timeout_seconds:
    overlapped I/O is used so ReadFile/WriteFile honor the deadline, not just WaitNamedPipe.
    """
    deadline = time.monotonic() + timeout_seconds

    # Open the pipe (retrying through the WaitNamedPipe→CreateFile race that
    # otherwise raises ERROR_PIPE_BUSY when many clients arrive at once).
    handle = _connect_with_retry(pipe_name, deadline)

    try:
        # Switch to message-read mode (server is PIPE_TYPE_MESSAGE).
        win32pipe.SetNamedPipeHandleState(
            handle,
            win32pipe.PIPE_READMODE_MESSAGE,
            None,
            None,
        )

        message = json.dumps(payload).encode("utf-8")
        _async_write(handle, message, deadline)

        response_bytes = _async_read(handle, 64 * 1024, deadline)
        response = response_bytes.decode("utf-8").strip()

        # Parse response: "ALLOW", "BLOCK", or "BLOCK|reason"
        if response.startswith("BLOCK"):
            reason = ""
            if "|" in response:
                reason = response.split("|", 1)[1]
            return "BLOCK", reason
        if response.upper() == "ALLOW":
            return "ALLOW", ""

        raise ValueError(f"Unexpected pipe response: {response!r}")

    finally:
        win32file.CloseHandle(handle)


def _async_write(handle, data: bytes, deadline: float) -> None:
    """Write data with overlapped I/O, honoring the deadline. Raises TimeoutError on miss."""
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
    """Read up to max_bytes with overlapped I/O, honoring the deadline. Raises TimeoutError on miss."""
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
        # CancelIo cancels all pending I/O for the calling thread on this handle.
        # CancelIoEx (per-overlapped) is not exposed by pywin32; for our single-
        # in-flight-IO-per-call client, CancelIo is equivalent.
        try:
            win32file.CancelIo(handle)
        except pywintypes.error:
            pass
        raise TimeoutError(f"pipe {op} timed out")
    raise OSError(f"WaitForSingleObject returned unexpected status: {rc}")


def _connect_with_retry(pipe_name: str, deadline: float):
    """WaitNamedPipe + CreateFile, retrying on ERROR_PIPE_BUSY until deadline.

    WaitNamedPipe doesn't reserve an instance, so under load another client
    may grab the slot between our wait and our open — surfaced as ERROR_PIPE_BUSY
    (231) on CreateFile. The contract here is "open the pipe before the deadline";
    we keep trying until that succeeds or time runs out.
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Named pipe '{pipe_name}' not available before deadline"
            )
        wait_ms = max(1, min(500, int(remaining * 1000)))
        try:
            win32pipe.WaitNamedPipe(pipe_name, wait_ms)
        except pywintypes.error:
            # ERROR_FILE_NOT_FOUND (2): server not running yet, retry.
            # ERROR_SEM_TIMEOUT (121): all instances busy, retry.
            continue
        try:
            return win32file.CreateFile(
                pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,       # no sharing
                None,    # default security
                win32file.OPEN_EXISTING,
                win32con.FILE_FLAG_OVERLAPPED,
                None,
            )
        except pywintypes.error as exc:
            if exc.winerror == _ERROR_PIPE_BUSY:
                continue   # raced another client; loop and try again
            raise
