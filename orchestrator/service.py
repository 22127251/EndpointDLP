"""Phase D placeholder Windows service.

Registers as ``DLPAgent``; ``SvcDoRun`` logs and blocks on the stop event.
Phase E will fold in the real foreground loop (Supervisor + pipes +
session-aware spawn helper). For now, ``sc start DLPAgent`` succeeds and the
service idles; operators must run ``python -m orchestrator --foreground`` for
actual DLP work.

This module is invoked from two paths:

1. **SCM dispatch** (``binPath= "...python.exe -m orchestrator --service ..."``):
   ``__main__.py`` consumes ``--service`` via argparse and calls
   ``run_as_service``, which hands off to pywin32's
   ``PrepareToHostSingle`` + ``StartServiceCtrlDispatcher``.
2. **Direct debug** (``python -m orchestrator.service install`` /
   ``... debug`` / ``... remove``): the ``__main__`` block at the bottom
   calls ``HandleCommandLine`` for the standard pywin32 UX. The Phase D
   ``--install`` flow itself uses ``sc.exe create`` instead — see
   ``orchestrator/installer.py:_step_install_service`` for why.
"""
from __future__ import annotations

import logging
import socket

import servicemanager
import win32event
import win32service
import win32serviceutil

from orchestrator.logging_setup import configure_logging


class DLPAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "DLPAgent"
    _svc_display_name_ = "DLP Endpoint Agent"
    _svc_description_ = (
        "Endpoint DLP orchestrator (Phase D placeholder; the service is "
        "registered but performs no DLP work yet. Run "
        "`python -m orchestrator --foreground` until Phase E ships.")

    def __init__(self, args):
        super().__init__(args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        socket.setdefaulttimeout(60)

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self) -> None:
        configure_logging(foreground=False)
        log = logging.getLogger("orchestrator.service")
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_WARNING_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_,
             " (Phase D PLACEHOLDER: run `python -m orchestrator --foreground` "
             "for actual DLP enforcement)"))
        log.warning(
            "DLPAgent placeholder started. Phase E replaces this body with the "
            "real Supervisor + session-aware spawning.")
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
        log.info("DLPAgent placeholder stopped.")


def run_as_service() -> None:
    """Entry point from ``python -m orchestrator --service`` (SCM dispatch).

    When the SCM launches our service, argparse in ``__main__.py`` has already
    consumed ``--service`` and ``--config``. We then hand off to pywin32's
    in-process SCM dispatcher, which spawns the service worker thread and
    calls ``DLPAgentService.SvcDoRun``.
    """
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(DLPAgentService)
    servicemanager.StartServiceCtrlDispatcher()


if __name__ == "__main__":
    # Direct invocation for in-place debugging: `python -m orchestrator.service debug`.
    # Phase D --install uses sc.exe directly, not this path.
    win32serviceutil.HandleCommandLine(DLPAgentService)
