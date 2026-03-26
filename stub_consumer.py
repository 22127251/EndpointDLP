"""
Reference pipe server — for testing the DLP addon.

Usage:
    python stub_consumer.py --decision allow
    python stub_consumer.py --decision block

The server loops forever, accepting one connection at a time,
reading the JSON payload from the addon, printing it, and
writing back the configured decision.
"""

import argparse
import json
import sys

import pywintypes
import win32event
import win32file
import win32pipe

PIPE_NAME = r"\\.\pipe\dlp_upload"
BUFFER_SIZE = 64 * 1024


def run(decision: str) -> None:
    decision = decision.upper()
    if decision not in ("ALLOW", "BLOCK"):
        print(f"Invalid decision '{decision}'. Use 'allow' or 'block'.", file=sys.stderr)
        sys.exit(1)

    print(f"[stub] Pipe server starting on {PIPE_NAME}")
    print(f"[stub] Will respond: {decision}")
    print("[stub] Ctrl-C to stop\n")

    try:
        while True:
            handle = win32pipe.CreateNamedPipe(
                PIPE_NAME,
                win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                1,           # max instances (serialize — one at a time)
                BUFFER_SIZE,
                BUFFER_SIZE,
                0,           # default timeout
                None,        # default security
            )

            try:
                _wait_for_client(handle)
                _, data = win32file.ReadFile(handle, BUFFER_SIZE)
                payload = json.loads(data.decode("utf-8"))

                print("[stub] Received upload:")
                print(json.dumps(payload, indent=2))
                print(f"[stub] → Sending: {decision}\n")

                win32file.WriteFile(handle, decision.encode("utf-8"))

            except pywintypes.error as e:
                print(f"[stub] Pipe error: {e}", file=sys.stderr)
            finally:
                win32file.CloseHandle(handle)

    except KeyboardInterrupt:
        print("\n[stub] Stopped.")


def _wait_for_client(handle) -> None:
    """
    Wait for a client to connect using overlapped I/O so that Ctrl-C
    (KeyboardInterrupt) can interrupt the wait between 500 ms polling ticks.
    """
    event = win32event.CreateEvent(None, True, False, None)
    try:
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = event

        try:
            win32pipe.ConnectNamedPipe(handle, overlapped)
        except pywintypes.error as e:
            # ERROR_IO_PENDING (997) means the overlapped call is in progress — expected
            # ERROR_PIPE_CONNECTED (535) means a client connected before we even waited
            if e.winerror == 535:
                return
            if e.winerror != 997:
                raise

        while True:
            rc = win32event.WaitForSingleObject(event, 500)  # 500 ms tick
            if rc == win32event.WAIT_OBJECT_0:
                return   # client connected
            # WAIT_TIMEOUT — loop again; Python checks for KeyboardInterrupt here
    finally:
        win32file.CloseHandle(event)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DLP stub pipe server")
    parser.add_argument(
        "--decision",
        choices=["allow", "block"],
        default="allow",
        help="Decision to return for every upload (default: allow)",
    )
    args = parser.parse_args()
    run(args.decision)
