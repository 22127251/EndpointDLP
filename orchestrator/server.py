from __future__ import annotations

import json
import logging
import os
import threading
import uuid

import pywintypes
import win32file
import win32pipe

from orchestrator.config import OrchestratorConfig
from orchestrator.dispatcher import Dispatcher
from orchestrator.pipe_security import build_pipe_sa

log = logging.getLogger(__name__)

_BUFFER = 65536
# ERROR_PIPE_CONNECTED: client connected between CreateNamedPipe and ConnectNamedPipe.
# This is a success condition — the pipe is already connected.
_ERROR_PIPE_CONNECTED = 535


class PipeServer:
    def __init__(self, config: OrchestratorConfig, dispatcher: Dispatcher) -> None:
        self._config = config
        self._dispatcher = dispatcher
        self._stop = threading.Event()
        # Built once; Win32 reads the descriptor bytes during CreateNamedPipe
        # and the PySECURITY_ATTRIBUTES object is reference-stable. Data-pipe
        # grants Authenticated Users RW (medium-integrity TransferAgent client).
        self._pipe_sa = build_pipe_sa(allow_authenticated_users=True)

    def run(self) -> None:
        log.info(
            "Pipe server listening on %s (%d accept threads)",
            self._config.data_pipe,
            self._config.pipe_listeners,
        )
        threads = [
            threading.Thread(
                target=self._accept_loop,
                daemon=True,
                name=f"pipe-accept-{i}",
            )
            for i in range(self._config.pipe_listeners)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        log.info("Pipe server stopped.")

    def stop(self) -> None:
        self._stop.set()
        # Unblock each blocked ConnectNamedPipe with a throwaway client connection.
        for _ in range(self._config.pipe_listeners):
            try:
                h = win32file.CreateFile(
                    self._config.data_pipe,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None,
                    win32file.OPEN_EXISTING,
                    0, None,
                )
                win32file.CloseHandle(h)
            except pywintypes.error:
                pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            # Create a new pipe instance for this iteration.
            # PIPE_UNLIMITED_INSTANCES avoids the nMaxInstances race where all
            # slots are taken by the other accept threads + in-flight handles.
            try:
                handle = win32pipe.CreateNamedPipe(
                    self._config.data_pipe,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    _BUFFER,
                    _BUFFER,
                    0,
                    self._pipe_sa,   # Phase C post-impl fix #1: grant Authenticated Users RW
                )
            except pywintypes.error as exc:
                log.error("CreateNamedPipe failed: %s", exc)
                break

            # Wait for a client to connect.
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as exc:
                if exc.winerror == _ERROR_PIPE_CONNECTED:
                    # Client connected between CreateNamedPipe and ConnectNamedPipe.
                    # Pipe is already connected — proceed normally.
                    pass
                else:
                    win32file.CloseHandle(handle)
                    continue

            if self._stop.is_set():
                try:
                    win32pipe.DisconnectNamedPipe(handle)
                    win32file.CloseHandle(handle)
                except pywintypes.error:
                    pass
                return

            self._handle_connection(handle)

    def _handle_connection(self, handle) -> None:
        """Read one request, analyze it, write the response, close the handle."""
        try:
            _, data = win32file.ReadFile(handle, _BUFFER)
        except Exception as exc:
            log.warning("ReadFile failed: %s", exc)
            _close_pipe(handle)
            return

        try:
            request = json.loads(data.decode("utf-8"))
        except Exception as exc:
            log.warning("JSON parse failed: %s", exc)
            _close_pipe(handle)
            return

        req_id = uuid.uuid4().hex[:8]
        request["req_id"] = req_id
        if request.get("kind") == "text":
            size = len(request.get("text", ""))
        else:
            fp = request.get("file_path", "")
            size = os.path.getsize(fp) if fp and os.path.exists(fp) else 0
        log.debug("recv req=%s channel=%s kind=%s size=%d",
                  req_id, request.get("channel"), request.get("kind"), size)

        try:
            decision, write_response, reason = self._dispatcher.analyze(request)
        except Exception as exc:
            log.error("Dispatcher error: %s", exc)
            decision, write_response, reason = "BLOCK", True, "Analysis error"

        if write_response:
            try:
                response = decision
                if decision == "BLOCK" and reason:
                    response = f"BLOCK|{reason}"
                win32file.WriteFile(handle, response.encode("utf-8"))
                win32file.FlushFileBuffers(handle)
            except Exception as exc:
                log.warning("WriteFile failed: %s", exc)

        _close_pipe(handle)


def _close_pipe(handle) -> None:
    try:
        win32pipe.DisconnectNamedPipe(handle)
    except pywintypes.error:
        pass
    try:
        win32file.CloseHandle(handle)
    except pywintypes.error:
        pass
