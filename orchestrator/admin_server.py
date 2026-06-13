"""Phase F admin-pipe: request/response control channel for ``dlp-ctl``.

A single-instance, low-volume named pipe (``config.admin_pipe``) restricted to
SYSTEM + BUILTIN\\Administrators (see :func:`orchestrator.pipe_security.build_pipe_sa`
with ``allow_authenticated_users=False``). A non-admin caller gets ACCESS_DENIED
when it tries to open the pipe — that is the intended hardening.

Protocol: the client opens the pipe, writes one JSON request, reads one JSON
response, and closes. Commands:

* ``{"cmd": "status"}`` → ``{"ok": true, ...status_provider()...}``
* ``{"cmd": "reload"}``  → ``{"ok": true, ...reload_callback()...}``
* anything else          → ``{"ok": false, "error": "unknown cmd: ..."}``

The accept loop mirrors ``server.PipeServer`` (blocking ``ConnectNamedPipe`` on a
daemon thread; ``stop()`` unblocks it with a throwaway self-connect), but a single
accept thread is plenty for operator traffic.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Callable

import pywintypes
import win32file
import win32pipe

from orchestrator.config import OrchestratorConfig
from orchestrator.pipe_security import build_pipe_sa

log = logging.getLogger(__name__)

_BUFFER = 65536
_ERROR_PIPE_CONNECTED = 535


class AdminServer:
    def __init__(
        self,
        config: OrchestratorConfig,
        status_provider: Callable[[], dict],
        reload_callback: Callable[[], dict],
        commands: dict[str, Callable[[dict], dict]] | None = None,
    ) -> None:
        self._config = config
        self._status_provider = status_provider
        self._reload_callback = reload_callback
        # Extensible command table (Phase AC-4): each entry maps a cmd name to a
        # handler taking the parsed request and returning a dict merged into the
        # {"ok": True, ...} response. status/reload stay special-cased above so the
        # existing callers/tests are unaffected; new commands (appcontrol_disable,
        # future server pushes) are one-line dict entries wired in __main__.
        self._commands = commands or {}
        self._stop = threading.Event()
        # Admins-only: non-admin dlp-ctl callers are denied at CreateFile.
        self._pipe_sa = build_pipe_sa(allow_authenticated_users=False)

    def run(self) -> None:
        log.info("Admin pipe listening on %s (Administrators only)", self._config.admin_pipe)
        while not self._stop.is_set():
            try:
                handle = win32pipe.CreateNamedPipe(
                    self._config.admin_pipe,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    _BUFFER, _BUFFER,
                    0, self._pipe_sa,
                )
            except pywintypes.error as exc:
                log.error("Admin CreateNamedPipe failed: %s", exc)
                break

            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as exc:
                if exc.winerror != _ERROR_PIPE_CONNECTED:
                    win32file.CloseHandle(handle)
                    continue

            if self._stop.is_set():
                _close(handle)
                return

            self._handle_connection(handle)
        log.info("Admin pipe server stopped.")

    def stop(self) -> None:
        self._stop.set()
        # Unblock the accept thread's ConnectNamedPipe with a throwaway connect.
        try:
            h = win32file.CreateFile(
                self._config.admin_pipe,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None,
                win32file.OPEN_EXISTING,
                0, None,
            )
            win32file.CloseHandle(h)
        except pywintypes.error:
            pass

    # -- internals ---------------------------------------------------------

    def _handle_connection(self, handle) -> None:
        try:
            _, data = win32file.ReadFile(handle, _BUFFER)
        except pywintypes.error as exc:
            log.warning("Admin ReadFile failed: %s", exc)
            _close(handle)
            return

        try:
            request = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._respond(handle, {"ok": False, "error": f"parse failed: {exc}"})
            return

        self._respond(handle, self.handle_request(request))

    def handle_request(self, request: dict) -> dict:
        """Map a parsed admin request to a response dict (pure; no I/O)."""
        cmd = request.get("cmd")
        try:
            if cmd == "status":
                return {"ok": True, **self._status_provider()}
            if cmd == "reload":
                return {"ok": True, **self._reload_callback()}
            handler = self._commands.get(cmd)
            if handler is not None:
                return {"ok": True, **handler(request)}
            return {"ok": False, "error": f"unknown cmd: {cmd!r}"}
        except Exception as exc:  # noqa: BLE001 — never crash the admin loop
            log.exception("Admin command %r failed", cmd)
            return {"ok": False, "error": f"command failed: {exc}"}

    def _respond(self, handle, response: dict) -> None:
        try:
            win32file.WriteFile(handle, json.dumps(response).encode("utf-8"))
            win32file.FlushFileBuffers(handle)
        except pywintypes.error as exc:
            log.warning("Admin WriteFile failed: %s", exc)
        _close(handle)


def _close(handle) -> None:
    try:
        win32pipe.DisconnectNamedPipe(handle)
    except pywintypes.error:
        pass
    try:
        win32file.CloseHandle(handle)
    except pywintypes.error:
        pass
