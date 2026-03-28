"""
Stub agent core — named pipe client for isolated analyzer testing.

Sends text chunks to the analyzer pipe server and prints the analysis result.
Measures and displays round-trip time (RTT) per chunk.

Usage:
    # Single text snippet
    python stub_agent_core.py --text "John Smith card 4111111111111111" --channel clipboard

    # Chunk a file (500 words per chunk) and send all chunks
    python stub_agent_core.py --file report.txt --channel browser

    # Interactive mode (no args): type text, press Enter, see result + RTT
    python stub_agent_core.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import pywintypes
import win32file
import win32pipe

PIPE_NAME = r"\\.\pipe\dlp_analyzer"
BUFFER_SIZE = 64 * 1024
CHUNK_WORDS = 500


# ---------------------------------------------------------------------------
# Pipe client
# ---------------------------------------------------------------------------

def send_chunk(payload: dict, pipe_name: str = PIPE_NAME, timeout_seconds: float = 30.0) -> dict:
    """Send a chunk JSON request; return the parsed JSON response."""
    deadline = time.monotonic() + timeout_seconds
    _wait_for_pipe(pipe_name, timeout_seconds)

    handle = win32file.CreateFile(
        pipe_name,
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0, None,
        win32file.OPEN_EXISTING,
        0, None,
    )
    try:
        win32pipe.SetNamedPipeHandleState(
            handle, win32pipe.PIPE_READMODE_MESSAGE, None, None,
        )
        message = json.dumps(payload).encode("utf-8")
        win32file.WriteFile(handle, message)
        _, response_bytes = win32file.ReadFile(handle, BUFFER_SIZE)
        return json.loads(response_bytes.decode("utf-8"))
    finally:
        win32file.CloseHandle(handle)


def _wait_for_pipe(pipe_name: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            win32pipe.WaitNamedPipe(pipe_name, 500)
            return
        except pywintypes.error as e:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Analyzer pipe '{pipe_name}' not available after {timeout_seconds}s"
                ) from e


# ---------------------------------------------------------------------------
# Chunk helpers
# ---------------------------------------------------------------------------

def _chunk_text(text: str, chunk_words: int = CHUNK_WORDS) -> list[str]:
    words = text.split()
    return [
        " ".join(words[i: i + chunk_words])
        for i in range(0, max(len(words), 1), chunk_words)
    ] or [""]


def _build_request(text: str, channel: str, sequence: int, total: int) -> dict:
    return {
        "chunk_id": str(uuid.uuid4()),
        "text": text,
        "metadata": {
            "channel": channel,
            "source_file": None,
            "sequence": sequence,
            "total_chunks": total,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        },
    }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _print_result(result: dict, rtt_ms: float) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[RTT: {rtt_ms:.1f}ms]\n")


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def run_text(text: str, channel: str) -> None:
    chunks = _chunk_text(text)
    total = len(chunks)
    overall_start = time.monotonic()
    for i, chunk in enumerate(chunks, 1):
        req = _build_request(chunk, channel, i, total)
        t0 = time.monotonic()
        result = send_chunk(req)
        rtt_ms = (time.monotonic() - t0) * 1000
        _print_result(result, rtt_ms)
    if total > 1:
        total_ms = (time.monotonic() - overall_start) * 1000
        print(f"[Total: {total} chunks, {total_ms:.1f}ms, avg {total_ms/total:.1f}ms/chunk]")


def run_file(path: str, channel: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    print(f"[File: {path} | {len(text)} chars]\n")
    run_text(text, channel)


def run_interactive(channel: str) -> None:
    print(f"[Interactive mode | channel={channel}]")
    print("Type text and press Enter. Empty line to quit.\n")
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\n[Stopped]")
            break
        if not line.strip():
            break
        req = _build_request(line, channel, 1, 1)
        t0 = time.monotonic()
        result = send_chunk(req)
        rtt_ms = (time.monotonic() - t0) * 1000
        _print_result(result, rtt_ms)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DLP Analyzer stub agent core")
    parser.add_argument("--channel", default="clipboard",
                        choices=["clipboard", "browser", "peripheral"],
                        help="Source channel (default: clipboard)")
    parser.add_argument("--text", help="Text to analyze (single request or chunked if long)")
    parser.add_argument("--file", help="Path to a text file to chunk and analyze")
    parser.add_argument("--pipe", default=PIPE_NAME, help="Named pipe path")
    args = parser.parse_args()

    PIPE_NAME = args.pipe  # override global if --pipe specified

    try:
        if args.file:
            run_file(args.file, args.channel)
        elif args.text:
            run_text(args.text, args.channel)
        else:
            run_interactive(args.channel)
    except TimeoutError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        print("[ERROR] Is the analyzer pipe server running? Start it with: python pipe_server.py",
              file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[Stopped]")
