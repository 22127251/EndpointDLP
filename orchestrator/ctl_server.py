"""Ctl-pipe server: push-based config hot-reload.

Single-instance subscriber per component (controller / clipboard / browser).
On every config.yaml change, ConfigWatcher invokes broadcast() which projects
each component's section out of the parsed yaml and pushes a config_update
message to the subscribed handle.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from typing import Callable

import pywintypes
import win32event
import win32file
import win32pipe

from orchestrator.config import OrchestratorConfig
from orchestrator.pipe_security import build_pipe_sa

log = logging.getLogger(__name__)

_BUFFER = 65536
_ERROR_PIPE_CONNECTED = 535
_ERROR_IO_PENDING = 997
_ERROR_BROKEN_PIPE = 109
_BROADCAST_WRITE_TIMEOUT_MS = 500
_STOP_POLL_MS = 200

# Phase F: the ctl-pipe carries config-push to the interceptor clients, one of
# which (the per-session ClipboardInterceptor) runs under a plain, non-admin
# user token and opens the pipe PipeDirection.InOut — so it needs RW. We grant
# an explicit DACL (SYSTEM + Administrators full, Authenticated Users RW) rather
# than relying on the default same-user SD, which could silently deny a
# non-admin session's subscribe. Privileged dlp-ctl commands live on the
# separate Administrators-only admin-pipe (see admin_server.py).

_KNOWN_COMPONENTS = ("controller", "clipboard", "browser")


def _project_section(raw: dict, component: str) -> dict:
    """Build the per-component config payload.

    Includes top-level pipe names (clients need them for bootstrap) plus the
    component's own subtree. The orchestrator's selective-skip handler in
    __main__._handle_config_change has already overridden data_pipe / ctl_pipe
    in `raw` back to the in-use values if the operator changed them in the yaml.
    So the emitted payload always carries the runtime pipe names, never a stale
    yaml value.
    """
    section_key = "peripheral_storage" if component == "controller" else component
    return {
        "data_pipe": raw.get("data_pipe", ""),
        "ctl_pipe": raw.get("ctl_pipe", ""),
        section_key: copy.deepcopy(raw.get(section_key, {})),
    }


class _SubHandle:
    __slots__ = ("handle", "pid")

    def __init__(self, handle, pid: int) -> None:
        self.handle = handle
        self.pid = pid


class CtlServer:
    """Push-based config server over a duplex message-mode named pipe."""

    def __init__(
        self,
        config: OrchestratorConfig,
        raw_provider: Callable[[], dict],
    ) -> None:
        self._config = config
        self._raw_provider = raw_provider
        self._subscribers: dict[str, _SubHandle] = {}
        self._sub_lock = threading.Lock()
        self._stop = threading.Event()
        # Built once; reference-stable for repeated CreateNamedPipe calls.
        self._pipe_sa = build_pipe_sa(allow_authenticated_users=True)

    def run(self) -> None:
        log.info("Ctl pipe listening on %s", self._config.ctl_pipe)
        while not self._stop.is_set():
            handle = self._create_pipe()
            if handle is None:
                break
            if not self._wait_for_client(handle):
                continue
            if self._stop.is_set():
                _close(handle)
                return
            threading.Thread(
                target=self._worker_loop,
                args=(handle,),
                daemon=True,
                name="ctl-worker",
            ).start()
        log.info("Ctl pipe server stopped.")

    def stop(self) -> None:
        self._stop.set()
        # Unblock the accept thread's ConnectNamedPipe wait with a throwaway connect.
        try:
            h = win32file.CreateFile(
                self._config.ctl_pipe,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None,
                win32file.OPEN_EXISTING,
                0, None,
            )
            win32file.CloseHandle(h)
        except pywintypes.error:
            pass
        # Close all subscriber handles so worker ReadFile loops break out.
        with self._sub_lock:
            for sub in list(self._subscribers.values()):
                try:
                    win32file.CloseHandle(sub.handle)
                except pywintypes.error:
                    pass
            self._subscribers.clear()

    def broadcast(self, components: tuple[str, ...] = _KNOWN_COMPONENTS) -> None:
        """Push a config_update to every component that has a live subscriber."""
        raw = self._raw_provider()
        version = int(time.time())
        with self._sub_lock:
            targets = [
                (component, self._subscribers[component])
                for component in components
                if component in self._subscribers
            ]
        if not targets:
            log.debug("Config changed but no ctl subscribers attached.")
            return
        summary = ", ".join(f"{component}=1" for component, _ in targets)
        log.info(
            "Broadcasting config_update to %d subscribers (%s)", len(targets), summary,
        )
        for component, sub in targets:
            self._send_update(component, sub, raw, version, kind="config_update")

    # -- internals ---------------------------------------------------------

    def _create_pipe(self):
        try:
            return win32pipe.CreateNamedPipe(
                self._config.ctl_pipe,
                win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES,
                _BUFFER, _BUFFER,
                0, self._pipe_sa,
            )
        except pywintypes.error as exc:
            log.error("Ctl pipe CreateNamedPipe failed: %s", exc)
            return None

    def _wait_for_client(self, handle) -> bool:
        """Overlapped ConnectNamedPipe with cooperative stop polling.

        Returns True iff a client connected; False on stop or error.
        """
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            try:
                win32pipe.ConnectNamedPipe(handle, overlapped)
            except pywintypes.error as exc:
                if exc.winerror == _ERROR_PIPE_CONNECTED:
                    return True
                if exc.winerror != _ERROR_IO_PENDING:
                    log.warning("Ctl ConnectNamedPipe failed: %s", exc)
                    _close(handle)
                    return False
            while not self._stop.is_set():
                rc = win32event.WaitForSingleObject(overlapped.hEvent, _STOP_POLL_MS)
                if rc == win32event.WAIT_OBJECT_0:
                    return True
            try:
                win32file.CancelIo(handle)
            except pywintypes.error:
                pass
            _close(handle)
            return False
        finally:
            win32file.CloseHandle(overlapped.hEvent)

    def _worker_loop(self, handle) -> None:
        component: str | None = None
        try:
            sub_msg = self._read_message(handle)
            if sub_msg is None:
                return
            if sub_msg.get("type") != "subscribe":
                self._send_error(handle, "parse_failed", "expected subscribe as first message")
                return
            component = sub_msg.get("component")
            if component not in _KNOWN_COMPONENTS:
                self._send_error(
                    handle, "unknown_component", f"unknown component: {component!r}",
                )
                component = None
                return
            pid_raw = sub_msg.get("pid", 0)
            try:
                pid = int(pid_raw)
            except (TypeError, ValueError):
                pid = 0
            sub = _SubHandle(handle=handle, pid=pid)
            with self._sub_lock:
                existing = self._subscribers.get(component)
                if existing is not None:
                    self._send_error(
                        handle, "already_subscribed",
                        f"component={component} already has a subscriber (pid={existing.pid})",
                    )
                    component = None  # don't prune the existing one in finally
                    return
                self._subscribers[component] = sub
            log.info("ctl: subscribed component=%s pid=%s", component, pid)

            if sub_msg.get("snapshot_request"):
                self._send_update(
                    component, sub, self._raw_provider(), int(time.time()),
                    kind="config_snapshot",
                )

            # Connection-death detector. The client never sends another message
            # in Phase B; this read either blocks until EOF or returns broken-pipe.
            while not self._stop.is_set():
                msg = self._read_message(handle)
                if msg is None:
                    break
                log.debug("ctl: unexpected message from %s: %r", component, msg)
        finally:
            if component is not None:
                with self._sub_lock:
                    cur = self._subscribers.get(component)
                    if cur is not None and cur.handle == handle:
                        del self._subscribers[component]
                        log.info("ctl: unsubscribed component=%s", component)
            _close(handle)

    def _send_update(
        self, component: str, sub: _SubHandle, raw: dict, version: int, *, kind: str,
    ) -> None:
        msg = {
            "type": kind,
            "section": component,
            "config": _project_section(raw, component),
            "version": version,
        }
        try:
            self._write_message_bounded(sub.handle, msg, _BROADCAST_WRITE_TIMEOUT_MS)
        except (TimeoutError, OSError, pywintypes.error) as exc:
            log.warning(
                "ctl: %s push to component=%s failed: %s — worker will clean up on next read",
                kind, component, exc,
            )

    def _send_error(self, handle, code: str, message: str) -> None:
        log.info("ctl: rejecting subscribe code=%s message=%s", code, message)
        try:
            self._write_message_bounded(
                handle,
                {"type": "error", "code": code, "message": message},
                _BROADCAST_WRITE_TIMEOUT_MS,
            )
        except (TimeoutError, OSError, pywintypes.error):
            pass

    def _read_message(self, handle) -> dict | None:
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            buf = win32file.AllocateReadBuffer(_BUFFER)
            try:
                win32file.ReadFile(handle, buf, overlapped)
            except pywintypes.error as exc:
                if exc.winerror == _ERROR_BROKEN_PIPE:
                    return None
                if exc.winerror != _ERROR_IO_PENDING:
                    log.warning("ctl: ReadFile failed: %s", exc)
                    return None
            while not self._stop.is_set():
                rc = win32event.WaitForSingleObject(overlapped.hEvent, _STOP_POLL_MS)
                if rc == win32event.WAIT_OBJECT_0:
                    try:
                        n = win32file.GetOverlappedResult(handle, overlapped, False)
                    except pywintypes.error as exc:
                        if exc.winerror == _ERROR_BROKEN_PIPE:
                            return None
                        log.warning("ctl: GetOverlappedResult failed: %s", exc)
                        return None
                    if n == 0:
                        return None
                    try:
                        return json.loads(bytes(buf[:n]).decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        log.warning("ctl: malformed JSON message: %s", exc)
                        return None
            try:
                win32file.CancelIo(handle)
            except pywintypes.error:
                pass
            return None
        finally:
            win32file.CloseHandle(overlapped.hEvent)

    def _write_message_bounded(self, handle, msg: dict, timeout_ms: int) -> None:
        data = json.dumps(msg).encode("utf-8")
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            try:
                win32file.WriteFile(handle, data, overlapped)
            except pywintypes.error as exc:
                if exc.winerror != _ERROR_IO_PENDING:
                    raise
            rc = win32event.WaitForSingleObject(overlapped.hEvent, timeout_ms)
            if rc == win32event.WAIT_TIMEOUT:
                try:
                    win32file.CancelIo(handle)
                except pywintypes.error:
                    pass
                raise TimeoutError(f"ctl: WriteFile exceeded {timeout_ms} ms")
            if rc != win32event.WAIT_OBJECT_0:
                raise OSError(f"WaitForSingleObject returned {rc}")
            win32file.GetOverlappedResult(handle, overlapped, False)
        finally:
            win32file.CloseHandle(overlapped.hEvent)


def _close(handle) -> None:
    try:
        win32pipe.DisconnectNamedPipe(handle)
    except pywintypes.error:
        pass
    try:
        win32file.CloseHandle(handle)
    except pywintypes.error:
        pass
