import argparse
import logging
import sys
import threading
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
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--foreground", action="store_true", help="Run in foreground (console) mode")
    group.add_argument("--install",    action="store_true", help="Install service, cert, and proxy")
    group.add_argument("--uninstall",  action="store_true", help="Uninstall service, cert, and proxy")
    group.add_argument("--service",    action="store_true", help="Run as Windows Service (internal)")
    args = parser.parse_args()

    if args.foreground:
        _run_foreground()
    else:
        print("Not implemented yet.")
        sys.exit(1)


def _run_foreground() -> None:
    configure_logging(foreground=True)
    log = logging.getLogger("orchestrator")
    log.info("Starting DLP orchestrator (foreground)")

    config = load_config()
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
