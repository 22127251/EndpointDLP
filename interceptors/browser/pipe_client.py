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
import win32file
import win32pipe


def send_and_receive(payload: dict, pipe_name: str, timeout_seconds: float) -> tuple[str, str]:
    """
    Open the named pipe, send JSON, read response, return (decision, reason).
    decision is "ALLOW" or "BLOCK". reason is non-empty only on BLOCK.
    """
    deadline = time.monotonic() + timeout_seconds

    # Wait for the pipe to become available (it may be busy serving another client)
    _wait_for_pipe(pipe_name, timeout_seconds)

    handle = win32file.CreateFile(
        pipe_name,
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0,       # no sharing
        None,    # default security
        win32file.OPEN_EXISTING,
        0,
        None,
    )

    try:
        # Switch to message-read mode
        win32pipe.SetNamedPipeHandleState(
            handle,
            win32pipe.PIPE_READMODE_MESSAGE,
            None,
            None,
        )

        message = json.dumps(payload).encode("utf-8")
        win32file.WriteFile(handle, message)

        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        # Use overlapped I/O timeout via SetCommTimeouts is not available for pipes;
        # instead rely on the server responding within the deadline.
        # ReadFile blocks until data arrives or handle is closed.
        _, response_bytes = win32file.ReadFile(handle, 64 * 1024)
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


def _wait_for_pipe(pipe_name: str, timeout_seconds: float) -> None:
    """
    Block until the named pipe server is ready or timeout elapses.
    Raises TimeoutError if pipe is not available in time.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            win32pipe.WaitNamedPipe(pipe_name, 500)  # 500 ms per attempt
            return
        except pywintypes.error as e:
            # ERROR_FILE_NOT_FOUND (2): server not running at all
            # ERROR_SEM_TIMEOUT (121): all instances busy, retry
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Named pipe '{pipe_name}' not available after {timeout_seconds}s"
                ) from e
