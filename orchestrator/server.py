from __future__ import annotations

import json
import logging

import pywintypes
import win32file
import win32pipe

from orchestrator.config import OrchestratorConfig
from orchestrator.policy_manager import PolicyManager

log = logging.getLogger(__name__)

_BUFFER = 65536


class PipeServer:
    def __init__(self, config: OrchestratorConfig, policy_manager: PolicyManager) -> None:
        self._config = config
        self._policy_manager = policy_manager
        self._running = False
        self._pipe = None

    def run(self) -> None:
        self._pipe = win32pipe.CreateNamedPipe(
            self._config.data_pipe,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
            1,
            _BUFFER,
            _BUFFER,
            0,
            None,
        )
        self._running = True
        log.info("Pipe server listening on %s", self._config.data_pipe)

        while self._running:
            try:
                win32pipe.ConnectNamedPipe(self._pipe, None)
            except pywintypes.error:
                break

            try:
                _, data = win32file.ReadFile(self._pipe, _BUFFER)
                request = json.loads(data.decode("utf-8"))
                decision, _ = self._policy_manager.analyze(
                    channel=request["channel"],
                    kind=request["kind"],
                    text=request.get("text"),
                    file_path=request.get("file_path"),
                )
                win32file.WriteFile(self._pipe, decision.encode("utf-8"))
                # Wait until the client has read the response before disconnecting.
                # DisconnectNamedPipe discards unread data; FlushFileBuffers blocks
                # until the client-side ReadAsync consumes the bytes.
                win32file.FlushFileBuffers(self._pipe)
            except Exception as e:
                if self._running:  # suppress errors triggered by the shutdown sentinel
                    log.error("Request handling error: %s", e)
            finally:
                try:
                    win32pipe.DisconnectNamedPipe(self._pipe)
                except pywintypes.error:
                    pass

        try:
            win32file.CloseHandle(self._pipe)
        except pywintypes.error:
            pass
        log.info("Pipe server stopped.")

    def stop(self) -> None:
        self._running = False
        # Connect a throwaway client to unblock ConnectNamedPipe in the server thread.
        # Closing the handle from another thread does not reliably unblock it on Windows.
        if self._pipe is not None:
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
