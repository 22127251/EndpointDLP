"""
Named pipe server for the DLP analyzer engine.

Protocol (message-mode pipe):
  Client → Server : UTF-8 JSON  { chunk_id, text, metadata: { channel, ... } }
  Server → Client : UTF-8 JSON  { chunk_id, detected_language, applied_action, violations }

Serializes connections — one chunk at a time, matching the C# agent core's
pipe client expectations. Mirrors the structure of stub_consumer.py.

Usage:
    python pipe_server.py
    python pipe_server.py --config analyzer_config.yaml --policies policies.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import pywintypes
import win32event
import win32file
import win32pipe

from analyzer_service import create_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BUFFER_SIZE = 64 * 1024  # 64 KB


def run(config_path: str, policies_path: str) -> None:
    log.info("Initializing analyzer service...")
    service = create_service(config_path, policies_path)

    import yaml
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    pipe_name: str = cfg.get("pipe_name", r"\\.\pipe\dlp_analyzer")

    log.info("Pipe server starting on %s", pipe_name)
    log.info("Ctrl-C to stop\n")

    try:
        while True:
            handle = win32pipe.CreateNamedPipe(
                pipe_name,
                win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                1,            # max instances (serialize)
                BUFFER_SIZE,
                BUFFER_SIZE,
                0,
                None,
            )

            try:
                _wait_for_client(handle)

                _, data = win32file.ReadFile(handle, BUFFER_SIZE)
                request = json.loads(data.decode("utf-8"))

                chunk_id = request.get("chunk_id", "?")
                channel = request.get("metadata", {}).get("channel", "?")
                log.info("Received chunk_id=%s channel=%s", chunk_id, channel)

                result = service.analyze(request)
                response_bytes = json.dumps(result.to_dict()).encode("utf-8")
                win32file.WriteFile(handle, response_bytes)

                log.info(
                    "Responded chunk_id=%s lang=%s action=%s violations=%d",
                    result.chunk_id,
                    result.detected_language,
                    result.applied_action,
                    len(result.violations),
                )

            except pywintypes.error as e:
                log.error("Pipe error: %s", e)
            except json.JSONDecodeError as e:
                log.error("Invalid JSON from client: %s", e)
            except Exception as e:
                log.exception("Unexpected error handling request: %s", e)
            finally:
                win32file.CloseHandle(handle)

    except KeyboardInterrupt:
        log.info("Stopped.")


def _wait_for_client(handle) -> None:
    """Wait for a client using overlapped I/O so Ctrl-C remains responsive."""
    event = win32event.CreateEvent(None, True, False, None)
    try:
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = event
        try:
            win32pipe.ConnectNamedPipe(handle, overlapped)
        except pywintypes.error as e:
            if e.winerror == 535:   # ERROR_PIPE_CONNECTED
                return
            if e.winerror != 997:  # ERROR_IO_PENDING — expected for overlapped
                raise
        while True:
            rc = win32event.WaitForSingleObject(event, 500)
            if rc == win32event.WAIT_OBJECT_0:
                return
    finally:
        win32file.CloseHandle(event)


if __name__ == "__main__":
    _here = os.path.dirname(os.path.abspath(__file__))

    parser = argparse.ArgumentParser(description="DLP Analyzer pipe server")
    parser.add_argument(
        "--config",
        default=os.path.join(_here, "analyzer_config.yaml"),
        help="Path to analyzer_config.yaml",
    )
    parser.add_argument(
        "--policies",
        default=os.path.join(_here, "policies.yaml"),
        help="Path to policies.yaml",
    )
    args = parser.parse_args()
    run(args.config, args.policies)
