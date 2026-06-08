import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def configure_logging(foreground: bool) -> None:
    log_dir = os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "DLP", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "dlp-agent.log")

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s")

    file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    if foreground:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)

    # Phase F: dedicated structured decision log (events.jsonl). The message is
    # already a JSON line, so the formatter emits it verbatim; propagate=False
    # keeps these out of dlp-agent.log and the console.
    events_path = os.path.join(log_dir, "events.jsonl")
    events_handler = RotatingFileHandler(
        events_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    events_handler.setFormatter(logging.Formatter("%(message)s"))
    events_logger = logging.getLogger("dlp.events")
    events_logger.setLevel(logging.INFO)
    events_logger.propagate = False
    # Avoid stacking duplicate handlers if configure_logging is called twice.
    if not any(isinstance(h, RotatingFileHandler) for h in events_logger.handlers):
        events_logger.addHandler(events_handler)
