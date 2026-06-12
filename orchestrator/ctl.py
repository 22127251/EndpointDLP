"""dlp-ctl admin CLI: status, reload, tail commands.

Lightweight operator tool (no analyzer deps, so it runs from the bundled embed):

    python -m orchestrator.ctl status
    python -m orchestrator.ctl reload
    python -m orchestrator.ctl tail [--log] [-n N] [--follow]

`status` and `reload` talk to the orchestrator's Administrators-only admin-pipe
(``config.admin_pipe``), so they must be run from an **elevated** prompt. `tail`
is a pure client-side reader of the log files and needs no IPC.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import pywintypes
    import win32file
    import win32pipe
    import winerror
except ImportError as exc:
    # PF#6: the admin-pipe client needs pywin32, which only the bundled embed
    # Python has. A bare `python` on a clean endpoint hits this — point the
    # operator at the wrapper / embed instead of dumping a raw traceback.
    sys.stderr.write(
        f"dlp-ctl: this Python interpreter is missing pywin32 ({exc.name!r}).\n"
        "Run dlp-ctl with the agent's bundled Python:\n"
        "  dlp-ctl status                                   (new shell; install root is on PATH)\n"
        '  "%ProgramFiles%\\DLP\\dlp-ctl.cmd" status         (from anywhere)\n'
        '  "%ProgramFiles%\\DLP\\python\\python.exe" -m orchestrator.ctl status\n'
    )
    sys.exit(2)

from orchestrator.config import load_config

_BUFFER = 65536


def _send_admin(admin_pipe: str, request: dict, timeout_ms: int = 5000) -> dict:
    """Open the admin-pipe, send one JSON request, read one JSON response."""
    # Wait briefly if the single pipe instance is momentarily busy.
    try:
        win32pipe.WaitNamedPipe(admin_pipe, timeout_ms)
    except pywintypes.error:
        pass  # not fatal — CreateFile reports the real error below
    handle = win32file.CreateFile(
        admin_pipe,
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0, None,
        win32file.OPEN_EXISTING,
        0, None,
    )
    try:
        win32pipe.SetNamedPipeHandleState(
            handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        win32file.WriteFile(handle, json.dumps(request).encode("utf-8"))
        win32file.FlushFileBuffers(handle)
        _, data = win32file.ReadFile(handle, _BUFFER)
    finally:
        win32file.CloseHandle(handle)
    return json.loads(data.decode("utf-8"))


def _admin_call(admin_pipe: str, request: dict) -> dict | None:
    """Wrap _send_admin with friendly error messages; returns None on failure."""
    try:
        return _send_admin(admin_pipe, request)
    except pywintypes.error as exc:
        if exc.winerror in (winerror.ERROR_FILE_NOT_FOUND, winerror.ERROR_PIPE_BUSY):
            print("dlp-ctl: agent not running (admin-pipe not found). "
                  "Start it with `Start-Service DLPAgent` or "
                  "`python -m orchestrator --foreground`.", file=sys.stderr)
        elif exc.winerror == winerror.ERROR_ACCESS_DENIED:
            print("dlp-ctl: access denied — run dlp-ctl from an elevated "
                  "(Administrator) prompt.", file=sys.stderr)
        else:
            print(f"dlp-ctl: admin-pipe error: {exc}", file=sys.stderr)
        return None


def _cmd_status(admin_pipe: str) -> int:
    resp = _admin_call(admin_pipe, {"cmd": "status"})
    if resp is None:
        return 1
    if not resp.get("ok"):
        print(f"dlp-ctl: {resp.get('error', 'status failed')}", file=sys.stderr)
        return 1
    print(f"uptime       : {resp.get('uptime_seconds')}s "
          f"(started {resp.get('started_at')})")
    print(f"mode         : {'service' if resp.get('service_mode') else 'foreground'}")
    inflight = resp.get("inflight", {})
    print("in-flight    : " + ", ".join(f"{k}={v}" for k, v in inflight.items()))
    print(f"last reload  : config={resp.get('last_config_reload')} "
          f"policies={resp.get('last_policy_reload')}")
    children = resp.get("children", {})
    if not children:
        print("children     : (none)")
    else:
        print("children     :")
        for name, st in children.items():
            flags = []
            if st.get("given_up"):
                flags.append("GIVEN-UP")
            if st.get("crashes_in_window"):
                flags.append(f"crashes={st['crashes_in_window']}")
            sess = st.get("session_id")
            sess_s = f" session={sess}" if sess is not None else ""
            state = "alive" if st.get("alive") else "DOWN"
            pid = st.get("pid")
            pid_s = f" pid={pid}" if pid else ""
            print(f"  {name:<22} {state}{pid_s}{sess_s} "
                  f"{' '.join(flags)}".rstrip())
    # Phase AC-3: App Control (WDAC) channel summary (display-only; the
    # `appcontrol` authoring subcommands arrive in AC-4).
    ac = resp.get("app_control")
    if isinstance(ac, dict):
        if not ac.get("enabled", False):
            print("app-control  : disabled")
        elif not ac.get("running", False):
            print("app-control  : enabled (not running)")
        elif not ac.get("policy_guid"):
            print(f"app-control  : no policy (pending={ac.get('pending_inbox', 0)}, "
                  f"rejected={ac.get('rejected_count', 0)})"
                  + (f", last_error={ac['last_error']}" if ac.get("last_error") else ""))
        else:
            blocks = ac.get("blocks") or {}
            print(f"app-control  : policy={ac['policy_guid']} v{ac.get('version_ex')}, "
                  f"blocks={blocks.get('enforce', 0)}/{blocks.get('audit', 0)} "
                  f"(enforce/audit), pending={ac.get('pending_inbox', 0)}, "
                  f"rejected={ac.get('rejected_count', 0)}"
                  + (f", last_error={ac['last_error']}" if ac.get("last_error") else ""))
    return 0


def _cmd_reload(admin_pipe: str) -> int:
    resp = _admin_call(admin_pipe, {"cmd": "reload"})
    if resp is None:
        return 1
    if not resp.get("ok"):
        print(f"dlp-ctl: {resp.get('error', 'reload failed')}", file=sys.stderr)
        return 1
    reloaded = resp.get("reloaded", [])
    print("reloaded: " + (", ".join(reloaded) if reloaded else "nothing changed"))
    return 0


def _cmd_tail(log_dir: Path, use_agent_log: bool, n: int, follow: bool) -> int:
    path = log_dir / ("dlp-agent.log" if use_agent_log else "events.jsonl")
    if not path.exists():
        print(f"dlp-ctl: log not found: {path}", file=sys.stderr)
        return 1
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
        for line in lines[-n:]:
            sys.stdout.write(line)
        if not follow:
            return 0
        sys.stdout.flush()
        f.seek(0, os.SEEK_END)
        try:
            while True:
                chunk = f.readline()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.3)
        except KeyboardInterrupt:
            return 0


def _resolve_log_dir(config) -> Path:
    if config.log_dir:
        return Path(config.log_dir)
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "DLP" / "logs"


def main(argv: list[str] | None = None) -> int:
    # --config is accepted both before and after the subcommand. SUPPRESS keeps
    # the subparser copy from overwriting a value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS,
                        help="Path to config.yaml (defaults to the install/repo config).")
    parser = argparse.ArgumentParser("dlp-ctl", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", parents=[common],
                   help="Show agent uptime, in-flight counts, child states.")
    sub.add_parser("reload", parents=[common],
                   help="Reload config.yaml / policies.yaml if changed.")
    p_tail = sub.add_parser("tail", parents=[common],
                            help="Tail the decision log (events.jsonl).")
    p_tail.add_argument("--log", action="store_true",
                        help="Tail dlp-agent.log instead of events.jsonl.")
    p_tail.add_argument("-n", type=int, default=50, help="Lines to show (default 50).")
    p_tail.add_argument("--follow", action="store_true", help="Stream new lines.")
    args = parser.parse_args(argv)

    config = load_config(getattr(args, "config", None))

    if args.command == "status":
        return _cmd_status(config.admin_pipe)
    if args.command == "reload":
        return _cmd_reload(config.admin_pipe)
    if args.command == "tail":
        return _cmd_tail(_resolve_log_dir(config), args.log, args.n, args.follow)
    parser.error("no command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
