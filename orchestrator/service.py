"""Phase E LocalSystem Windows service body.

``DLPAgent`` runs as LocalSystem (Session 0). ``SvcDoRun`` drives the same
``run_core`` loop as ``--foreground`` (PolicyManager + Dispatcher + PipeServer +
CtlServer + ConfigWatcher + Supervisor) but in *service mode*, where the
Supervisor spawns per-session children (ClipboardInterceptor, and Controller if
the E0 spike said cross-session injection is unavailable) into each interactive
session via the session bridge. WTS session-change notifications (logon/logoff)
add and remove those per-session children + redirect/restore their proxy.

Invocation paths:

1. **SCM dispatch** (``binPath= "...python.exe -m orchestrator --service --config X"``):
   ``__main__.py`` parses ``--service``/``--config`` and calls
   :func:`run_as_service`, which stashes the config path and hands off to pywin32's
   ``PrepareToHostSingle`` + ``StartServiceCtrlDispatcher``. The service worker
   thread (same process) then runs :meth:`DLPAgentService.SvcDoRun`.
2. **Direct debug** (``python -m orchestrator.service debug`` / ``install`` /
   ``remove``): the ``__main__`` block calls ``HandleCommandLine``. The Phase D
   ``--install`` flow itself uses ``sc.exe create`` — see ``installer.py``.
"""
from __future__ import annotations

import logging
import socket
import threading
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

from orchestrator.logging_setup import configure_logging

# WTS session-change codes (wtsapi32.h). pywin32 does not export these, so we
# define the ones we act on. Values are stable Windows constants.
WTS_CONSOLE_CONNECT = 1
WTS_REMOTE_CONNECT = 3
WTS_SESSION_LOGON = 5
WTS_SESSION_LOGOFF = 6

# Set by run_as_service before SCM dispatch so the SCM-constructed service
# instance can read the --config value parsed in __main__.py.
_config_path: Path | None = None


class DLPAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "DLPAgent"
    _svc_display_name_ = "DLP Endpoint Agent"
    _svc_description_ = (
        "Endpoint DLP orchestrator. Runs as LocalSystem, supervises the "
        "interceptor processes across user sessions, and routes intercepted "
        "data to the content analyzer.")

    def __init__(self, args):
        super().__init__(args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self._stop_event = threading.Event()
        self._supervisor = None
        socket.setdefaulttimeout(60)

    def GetAcceptedControls(self):
        # Add session-change notifications to the default accepted controls so
        # SvcOtherEx receives logon / logoff events for per-session children.
        controls = super().GetAcceptedControls()
        controls |= win32service.SERVICE_ACCEPT_SESSIONCHANGE
        return controls

    def SvcStop(self) -> None:
        # Phase F: a generous wait hint so the SCM doesn't flag the bounded drain
        # (config service.drain_timeout_seconds ≤8 s) + child teardown (grace
        # ≤15 s) + thread joins as a hang. The checkpoint counter is managed
        # internally by pywin32's ReportServiceStatus.
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30000)
        self._stop_event.set()
        win32event.SetEvent(self.hWaitStop)

    def SvcOtherEx(self, control, event_type, data) -> None:
        if control != win32service.SERVICE_CONTROL_SESSIONCHANGE:
            return
        # data is a single-element tuple carrying the session id.
        session_id = data[0] if isinstance(data, (tuple, list)) else data
        log = logging.getLogger("orchestrator.service")
        if self._supervisor is None:
            log.warning("SESSIONCHANGE (type=%s session=%s) before supervisor ready; "
                        "ignoring", event_type, session_id)
            return
        try:
            if event_type in (WTS_SESSION_LOGON, WTS_CONSOLE_CONNECT, WTS_REMOTE_CONNECT):
                log.info("Session %s logon/connect (type=%s) → start_session",
                         session_id, event_type)
                self._supervisor.start_session(session_id)
            elif event_type == WTS_SESSION_LOGOFF:
                log.info("Session %s logoff → stop_session", session_id)
                self._supervisor.stop_session(session_id)
        except Exception:  # noqa: BLE001  — never let a handler crash the service
            log.exception("SESSIONCHANGE handling failed (type=%s session=%s)",
                          event_type, session_id)

    def SvcDoRun(self) -> None:
        configure_logging(foreground=False)
        # The SCM dispatcher (pywin32 StartServiceCtrlDispatcher) created this
        # thread outside Python's threading module, so logging would otherwise
        # label it "Dummy-1". Give it a readable name for the logs.
        threading.current_thread().name = "svc-main"
        log = logging.getLogger("orchestrator.service")
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""))
        log.info("DLPAgent service starting (LocalSystem, session-aware).")

        # Import here (not at module top) so the SCM dispatch path stays light and
        # any analyzer-dep import error surfaces in the log rather than killing the
        # process before StartServiceCtrlDispatcher — see __main__.py top comment.
        from orchestrator.__main__ import run_core

        def _capture(supervisor):
            self._supervisor = supervisor

        try:
            run_core(_config_path, self._stop_event,
                     foreground=False, ready_callback=_capture)
        except Exception:  # noqa: BLE001
            log.exception("DLPAgent run_core crashed.")
            raise
        finally:
            log.info("DLPAgent service stopped.")


def run_as_service(config_path: Path | None = None) -> None:
    """Entry point from ``python -m orchestrator --service`` (SCM dispatch).

    Stashes ``config_path`` for the SCM-constructed service instance, then hands
    off to pywin32's in-process SCM dispatcher (no PythonService.exe needed).
    """
    global _config_path
    _config_path = config_path
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(DLPAgentService)
    servicemanager.StartServiceCtrlDispatcher()


if __name__ == "__main__":
    # Direct invocation for in-place debugging: `python -m orchestrator.service debug`.
    win32serviceutil.HandleCommandLine(DLPAgentService)
