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

from orchestrator.config import load_config
from orchestrator.dispatcher import Dispatcher
from orchestrator.logging_setup import configure_logging
from orchestrator.policy_manager import PolicyManager
from orchestrator.server import PipeServer


def main() -> None:
    parser = argparse.ArgumentParser("python -m orchestrator")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to orchestrator.yaml (defaults to repo root).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--foreground", action="store_true", help="Run in foreground (console) mode")
    group.add_argument("--install",    action="store_true", help="Install service, cert, and proxy")
    group.add_argument("--uninstall",  action="store_true", help="Uninstall service, cert, and proxy")
    group.add_argument("--service",    action="store_true", help="Run as Windows Service (internal)")
    args = parser.parse_args()

    if args.foreground:
        _run_foreground(args.config)
    else:
        print("Not implemented yet.")
        sys.exit(1)


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
    original_analyze = PolicyManager.analyze

    def slow_analyze(self, *args, **kwargs):
        time.sleep(delay_s)
        return original_analyze(self, *args, **kwargs)

    PolicyManager.analyze = slow_analyze  # type: ignore[assignment]
    logging.getLogger("orchestrator").warning(
        "DLP_TEST_SLOW_MS=%s active — analyses will sleep before running.", raw
    )


def _run_foreground(config_path: Path | None = None) -> None:
    configure_logging(foreground=True)
    log = logging.getLogger("orchestrator")
    log.info("Starting DLP orchestrator (foreground)")

    _maybe_install_slow_test_hook()

    config = load_config(config_path)
    pm = PolicyManager(config)
    dispatcher = Dispatcher(config, pm)
    server = PipeServer(config, dispatcher)

    # Run the blocking pipe server on a daemon thread so that Ctrl+C (KeyboardInterrupt)
    # can interrupt the main thread's join() — signal handlers can't fire while a
    # blocking C call (ConnectNamedPipe) holds the main thread.
    t = threading.Thread(target=server.run, daemon=True, name="pipe-server")
    t.start()

    try:
        while t.is_alive():
            t.join(timeout=0.5)
    except KeyboardInterrupt:
        log.info("Ctrl+C received, shutting down...")
        server.stop()
        t.join(timeout=5.0)
    finally:
        dispatcher.shutdown(wait=True)
        pm.stop()
        log.info("Orchestrator stopped cleanly.")


if __name__ == "__main__":
    main()
