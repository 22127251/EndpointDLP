import argparse
import datetime
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
    from orchestrator.admin_server import AdminServer
    from orchestrator.app_control.channel import AppControlChannel
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

    start_monotonic = time.monotonic()
    start_wall = time.time()

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
    in_use_admin_pipe = config.admin_pipe
    in_use_server_section = config.raw.get("server", {}).copy()

    ctl_server = CtlServer(config, raw_provider=lambda: raw_cell["raw"])

    if config_path is None:
        watcher_path = Path(__file__).parent.parent / "config.yaml"
    else:
        watcher_path = Path(config_path)

    # Phase F: track the last config (re)load wall time for dlp-ctl status.
    config_state = {"reloaded_wall": start_wall}

    # Phase AC-3: the App Control channel (created after the admin server, below).
    # Declared here so the config-change handler + status provider can close over it
    # even though it starts later; it stays None until then (and when disabled).
    app_control_channel = None

    def _handle_config_change(new_raw: dict) -> None:
        new_data_pipe = new_raw.get("data_pipe")
        new_ctl_pipe = new_raw.get("ctl_pipe")
        new_admin_pipe = new_raw.get("admin_pipe")
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
        if new_admin_pipe != in_use_admin_pipe:
            log.warning(
                "admin_pipe change requires restart; keeping %r (yaml wanted %r)",
                in_use_admin_pipe, new_admin_pipe,
            )
        new_server = new_raw.get("server", {})
        if new_server.get("agent_id") != in_use_server_section.get("agent_id"):
            log.warning(
                "server.agent_id change requires restart; keeping %r",
                in_use_server_section.get("agent_id"),
            )
        # Override the unchangeable fields back to in-use values so subscribers
        # see an internally-consistent snapshot. Other field changes pass through.
        new_raw = {**new_raw, "data_pipe": in_use_data_pipe,
                   "ctl_pipe": in_use_ctl_pipe, "admin_pipe": in_use_admin_pipe,
                   "server": {**new_server, "agent_id": in_use_server_section.get("agent_id", ""),
                              "url": in_use_server_section.get("url", ""),
                              "enabled": in_use_server_section.get("enabled", False)}}
        # Apply the hot-reloadable orchestrator-side fields in place. The live
        # PolicyManager / Dispatcher / PipeServer share this `config` object by
        # reference, so the in-place swap is what makes a server-side reload take
        # effect without a restart; restart-only fields (pipe names, pools, paths,
        # proxy, …) are left frozen (see OrchestratorConfig.apply_hot_reload).
        try:
            changed = config.apply_hot_reload(new_raw)
            if changed:
                log.info("config hot-reload applied: %s", ", ".join(sorted(changed)))
        except Exception:  # noqa: BLE001 — a bad reload must not kill the watcher thread
            log.exception("config apply_hot_reload failed; keeping previous orchestrator config")
        raw_cell["raw"] = new_raw
        config_state["reloaded_wall"] = time.time()
        ctl_server.broadcast()
        if app_control_channel is not None:
            try:
                app_control_channel.apply_config(new_raw)
            except Exception:  # noqa: BLE001 — config errors must not break reload
                log.exception("app_control apply_config failed")

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

    # ── Cloud bridge: management console connectivity ──
    cloud_bridge = None
    if config.server_enabled and config.server_url:
        from orchestrator.cloud_bridge import CloudBridge
        cloud_bridge = CloudBridge(config)
        dispatcher.set_violation_callback(cloud_bridge.report_violation)
        cloud_bridge.start()

    if ready_callback is not None:
        # Hand the live supervisor to the service so SvcOtherEx can drive
        # start_session / stop_session on logon / logoff.
        ready_callback(supervisor)

    # ── Phase F: admin-pipe (dlp-ctl status / reload) ──
    def _iso(epoch: float) -> str:
        return datetime.datetime.fromtimestamp(
            epoch, datetime.timezone.utc).isoformat()

    def _status_provider() -> dict:
        return {
            "uptime_seconds": round(time.monotonic() - start_monotonic, 1),
            "started_at": _iso(start_wall),
            "service_mode": not foreground,
            "inflight": dispatcher.inflight_counts(),
            "last_config_reload": _iso(config_state["reloaded_wall"]),
            "last_policy_reload": _iso(pm.last_reload_time()),
            "children": supervisor.status_snapshot() if supervisor is not None else {},
            "app_control": (app_control_channel.status() if app_control_channel is not None
                            else {"enabled": config.app_control_enabled, "running": False}),
        }

    def _reload_callback() -> dict:
        # Force-reload (Option A): unconditionally re-apply BOTH files and report
        # what was applied. The file-watchers handle automatic apply on edit;
        # this manual command is the authoritative "apply now" (and the hook the
        # future central server calls after pushing config/policies). Per-file
        # failures are returned in `errors`.
        reloaded: list[str] = []
        errors: dict[str, str] = {}
        if pm.force_reload():
            reloaded.append("policies")
        else:
            errors["policies"] = "rebuild failed; kept previous policies (see dlp-agent.log)"
        try:
            import yaml
            with open(watcher_path, encoding="utf-8") as f:
                new_raw = yaml.safe_load(f)
            _handle_config_change(new_raw)  # re-applies + re-broadcasts
            reloaded.append("config")
        except Exception as exc:  # noqa: BLE001
            log.warning("reload: config reload failed: %s", exc)
            errors["config"] = str(exc)
        result: dict = {"reloaded": reloaded}
        if errors:
            result["errors"] = errors
        return result

    def _appcontrol_disable(request: dict) -> dict:
        # Phase AC-4: `dlp-ctl appcontrol disable` over the admin-pipe. Reads the
        # later-assigned app_control_channel at call time (like _status_provider).
        if app_control_channel is None:
            return {"removed": False, "error": "app control channel not running"}
        return app_control_channel.disable()

    admin_server = AdminServer(
        config, _status_provider, _reload_callback,
        commands={"appcontrol_disable": _appcontrol_disable})
    admin_thread = threading.Thread(
        target=admin_server.run, daemon=True, name="admin-server")
    admin_thread.start()

    # ── Phase AC-3: App Control (WDAC) channel — inbox watcher + deployer +
    # event forwarder as orchestrator-internal daemon threads. Gated by config and
    # by DLP_APPCONTROL_DISABLED (the harness opt-out; the channel needs LocalSystem
    # privilege, exercised on the VM, not in the pipe/dispatch subprocess fixtures).
    # Best-effort start: a failure here never stops the orchestrator.
    if config.app_control_enabled and not os.environ.get("DLP_APPCONTROL_DISABLED"):
        try:
            app_control_channel = AppControlChannel(config)
            app_control_channel.start()
        except Exception:  # noqa: BLE001 — channel is non-critical to the agent
            log.exception("App Control channel failed to start; continuing without it")
            app_control_channel = None
    else:
        log.info("App Control channel disabled (enabled=%s, env-opt-out=%s).",
                 config.app_control_enabled, bool(os.environ.get("DLP_APPCONTROL_DISABLED")))

    try:
        # Block until SvcStop sets stop_event, the pipe server dies, or (foreground)
        # Ctrl+C raises KeyboardInterrupt. The 0.5 s tick lets KeyboardInterrupt fire.
        while not stop_event.is_set() and t.is_alive():
            stop_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        log.info("Ctrl+C received, shutting down...")
        stop_event.set()
    finally:
        # Stop cloud bridge first (stop heartbeat before shutting down pipes)
        if cloud_bridge is not None:
            cloud_bridge.stop()
        # Stop children FIRST so Controller releases the alive mutex (hooks
        # deactivate) while the orchestrator's pipes are still up. (Proxy is
        # restored inside supervisor.stop_all()/stop_session().)
        if supervisor is not None:
            supervisor.stop_all()
        server.stop()
        ctl_server.stop()
        admin_server.stop()
        if app_control_channel is not None:
            app_control_channel.stop()
        t.join(timeout=5.0)
        ctl_thread.join(timeout=5.0)
        admin_thread.join(timeout=5.0)
        config_watcher.stop()
        # Phase F: bounded drain instead of an unbounded shutdown(wait=True), so
        # a stuck analysis can't hang SvcStop past the SCM timeout.
        abandoned = dispatcher.drain(config.drain_timeout_seconds)
        if abandoned:
            log.warning("drain: %d analyses abandoned after %ss",
                        abandoned, config.drain_timeout_seconds)
        pm.stop()
        log.info("Orchestrator stopped cleanly.")


if __name__ == "__main__":
    main()
