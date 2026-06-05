import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "analyzer"))  # engine.py uses bare 'from policy import'

# Top-level imports are intentionally minimal. Per-mode imports happen inside
# each dispatch branch below. Reason: the bundled `python-embed` only has the
# orchestrator's top-level requirements installed (mitmproxy + pywin32 +
# pyyaml + watchdog). The analyzer's deps (pyahocorasick, google-re2,
# PyMuPDF, …) live in analyzer\requirements.txt and are NOT in the embed.
# An eager `from orchestrator.policy_manager import PolicyManager` here would
# cascade into `from analyzer.engine import DLPEngine` → `import ahocorasick`,
# which raises ModuleNotFoundError from the bundled Python. That killed the
# SCM-launched --service before pywin32's StartServiceCtrlDispatcher ran,
# producing 1053 timeouts. Keeping these imports lazy lets --service start
# from the embed even when the analyzer deps aren't installed.


def main() -> None:
    parser = argparse.ArgumentParser("python -m orchestrator")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml (defaults to repo root).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--foreground", action="store_true", help="Run in foreground (console) mode")
    group.add_argument("--install",    action="store_true", help="Install service, cert, and proxy")
    group.add_argument("--uninstall",  action="store_true", help="Uninstall service, cert, and proxy")
    group.add_argument("--service",    action="store_true", help="Run as Windows Service (internal)")
    args = parser.parse_args()

    if args.foreground:
        _run_foreground(args.config)
    elif args.install:
        from orchestrator.installer import run_install
        sys.exit(run_install(args.config))
    elif args.uninstall:
        from orchestrator.installer import run_uninstall
        sys.exit(run_uninstall(args.config))
    elif args.service:
        # SCM dispatch — only returns when the service stops.
        from orchestrator.service import run_as_service
        run_as_service(args.config)
    else:
        parser.error("no mode selected; pass --foreground / --install / --uninstall / --service")


def _maybe_install_slow_test_hook() -> None:
    # DLP_TEST_SLOW_MS: harness affordance to deterministically slow down analysis.
    # When set, wraps PolicyManager.analyze with a leading sleep of N milliseconds.
    raw = os.environ.get("DLP_TEST_SLOW_MS")
    if not raw:
        return
    try:
        delay_s = float(raw) / 1000.0
    except ValueError:
        return
    # Lazy import — PolicyManager pulls analyzer deps that may not be installed
    # in every Python environment (notably the bundled embed).
    from orchestrator.policy_manager import PolicyManager
    original_analyze = PolicyManager.analyze

    def slow_analyze(self, *args, **kwargs):
        time.sleep(delay_s)
        return original_analyze(self, *args, **kwargs)

    PolicyManager.analyze = slow_analyze  # type: ignore[assignment]
    logging.getLogger("orchestrator").warning(
        "DLP_TEST_SLOW_MS=%s active — analyses will sleep before running.", raw
    )


def _run_foreground(config_path: Path | None = None) -> None:
    # Thin wrapper: configure console logging, then drive the shared run-core with
    # a stop event that Ctrl+C (KeyboardInterrupt inside run_core) trips.
    from orchestrator.logging_setup import configure_logging

    configure_logging(foreground=True)
    stop_event = threading.Event()
    run_core(config_path, stop_event, foreground=True)


def run_core(
    config_path: Path | None,
    stop_event: "threading.Event",
    *,
    foreground: bool,
    ready_callback=None,
) -> None:
    """Shared orchestrator run-loop for both --foreground and the Windows service.

    Builds PolicyManager / Dispatcher / PipeServer / CtlServer / ConfigWatcher /
    Supervisor and blocks until ``stop_event`` is set (service ``SvcStop``) or
    Ctrl+C is received (foreground). ``foreground`` selects the supervisor mode:
    foreground runs every child Session-local (Phase C); the service (foreground
    =False) runs in Session 0 and spawns per-session children via the session
    bridge. ``ready_callback`` (used by the service) is invoked with the live
    Supervisor right after start so SESSIONCHANGE events can drive start/stop_session.

    The caller configures logging before calling (console for foreground, file for
    the service). Heavy imports stay inside this function so --install / --uninstall
    / --service dispatch can run from the embed without analyzer deps — see the
    module-top comment block.
    """
    from orchestrator.config import load_config
    from orchestrator.config_watcher import ConfigWatcher
    from orchestrator.ctl_server import CtlServer
    from orchestrator.dispatcher import Dispatcher
    from orchestrator.policy_manager import PolicyManager
    from orchestrator.server import PipeServer
    from orchestrator.supervisor import Supervisor, build_default_specs

    log = logging.getLogger("orchestrator")
    log.info("Starting DLP orchestrator (%s)", "foreground" if foreground else "service")

    _maybe_install_slow_test_hook()

    config = load_config(config_path)
    pm = PolicyManager(config)
    dispatcher = Dispatcher(config, pm)
    server = PipeServer(config, dispatcher)

    # ── Phase B: ctl-pipe server + config.yaml hot-reload watcher ──
    # raw_cell holds the latest parsed yaml; ctl_server projects per-component
    # sections from it. _handle_config_change is the bridge from the watcher
    # to the broadcast — it implements decision #7's selective-skip for the
    # non-hot-reloadable data_pipe / ctl_pipe fields.
    raw_cell: dict[str, dict] = {"raw": config.raw}
    in_use_data_pipe = config.data_pipe
    in_use_ctl_pipe = config.ctl_pipe

    ctl_server = CtlServer(config, raw_provider=lambda: raw_cell["raw"])

    def _handle_config_change(new_raw: dict) -> None:
        new_data_pipe = new_raw.get("data_pipe")
        new_ctl_pipe = new_raw.get("ctl_pipe")
        if new_data_pipe != in_use_data_pipe:
            log.warning(
                "data_pipe change requires restart; keeping %r (yaml wanted %r)",
                in_use_data_pipe, new_data_pipe,
            )
        if new_ctl_pipe != in_use_ctl_pipe:
            log.warning(
                "ctl_pipe change requires restart; keeping %r (yaml wanted %r)",
                in_use_ctl_pipe, new_ctl_pipe,
            )
        # Override the unchangeable fields back to in-use values so subscribers
        # see an internally-consistent snapshot. Other field changes pass through.
        new_raw = {**new_raw, "data_pipe": in_use_data_pipe, "ctl_pipe": in_use_ctl_pipe}
        raw_cell["raw"] = new_raw
        ctl_server.broadcast()

    if config_path is None:
        watcher_path = Path(__file__).parent.parent / "config.yaml"
    else:
        watcher_path = Path(config_path)
    config_watcher = ConfigWatcher(watcher_path, on_change=_handle_config_change)
    config_watcher.start()

    ctl_thread = threading.Thread(target=ctl_server.run, daemon=True, name="ctl-server")
    ctl_thread.start()

    # Run the blocking pipe server on a daemon thread so that Ctrl+C (KeyboardInterrupt)
    # can interrupt the main thread's join() — signal handlers can't fire while a
    # blocking C call (ConnectNamedPipe) holds the main thread.
    t = threading.Thread(target=server.run, daemon=True, name="pipe-server")
    t.start()

    # ── Phase C: spawn and supervise the three child processes ──
    # Both pipes are bound by now, so children connecting at startup hit a
    # ready server. The client-side retry in CtlPipeSubscriber/OrchestratorClient
    # is the actual safety net against any residual race.
    # DLP_SUPERVISOR_DISABLED is the harness opt-out — the Phase A pytests
    # only need the orchestrator's pipe/dispatch/config-watch behavior, not
    # the supervised children.
    supervisor: Supervisor | None = None
    if not os.environ.get("DLP_SUPERVISOR_DISABLED"):
        repo_root = Path(__file__).parent.parent
        supervisor = Supervisor(
            config,
            repo_root=repo_root,
            specs=build_default_specs(config, repo_root),
            # Service mode (foreground=False) runs in Session 0 and spawns
            # per-session children via the session bridge; foreground keeps the
            # Phase C Session-local behavior.
            service_mode=not foreground,
        )
        supervisor.start_all()
        log.info(
            "Supervisor started (%s); supervising %d children.",
            "service" if not foreground else "foreground",
            len(supervisor.status_snapshot()),
        )
    else:
        log.info("DLP_SUPERVISOR_DISABLED set; skipping child supervisor.")

    if ready_callback is not None:
        # Hand the live supervisor to the service so SvcOtherEx can drive
        # start_session / stop_session on logon / logoff.
        ready_callback(supervisor)

    try:
        # Block until SvcStop sets stop_event, the pipe server dies, or (foreground)
        # Ctrl+C raises KeyboardInterrupt. The 0.5 s tick lets KeyboardInterrupt fire.
        while not stop_event.is_set() and t.is_alive():
            stop_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        log.info("Ctrl+C received, shutting down...")
        stop_event.set()
    finally:
        # Stop children FIRST so Controller releases the alive mutex (hooks
        # deactivate) while the orchestrator's pipes are still up.
        if supervisor is not None:
            supervisor.stop_all()
        server.stop()
        ctl_server.stop()
        t.join(timeout=5.0)
        ctl_thread.join(timeout=5.0)
        config_watcher.stop()
        dispatcher.shutdown(wait=True)
        pm.stop()
        log.info("Orchestrator stopped cleanly.")


if __name__ == "__main__":
    main()
