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


# --------------------------------------------------------------------------- #
# Phase AC-4: App Control (WDAC) authoring subcommands
# --------------------------------------------------------------------------- #

def _cmd_appcontrol(args, config) -> int:
    # Lazy import: keep status/reload/tail import-light. builder pulls only the
    # AC-2/AC-3 app_control modules (no analyzer deps), so this is embed-safe.
    from orchestrator.app_control import builder, paths

    cmd = args.ac_command
    if cmd in ("allow", "deny"):
        return _ac_list(builder, paths, config, cmd, args.paths, args.remove)
    if cmd == "build":
        return _ac_build(builder, config, getattr(args, "version", None))
    if cmd == "apply":
        return _ac_apply(builder, config)
    if cmd == "status":
        return _ac_status(builder, paths, config)
    if cmd == "disable":
        return _ac_disable(builder, config, args.force_local)
    return 2


def _ac_list(builder, paths, config, which: str, path_args, remove: bool) -> int:
    list_path = (paths.allow_list_path(config) if which == "allow"
                 else paths.deny_list_path(config))
    try:
        if remove:
            removed, remaining = builder.remove_entries(list_path, path_args)
            print(f"{which}: removed {len(removed)} ({len(remaining)} remain) -> {list_path}")
            for e in removed:
                print(f"  - {e}")
        else:
            added, all_entries = builder.add_entries(list_path, path_args)
            print(f"{which}: added {len(added)} ({len(all_entries)} total) -> {list_path}")
            for e in added:
                print(f"  + {e}")
        return 0
    except OSError as exc:
        print(f"dlp-ctl: failed to update {which}-list: {exc}", file=sys.stderr)
        return 1


def _ac_build(builder, config, version) -> int:
    try:
        result = builder.build(config, version=version)
    except (RuntimeError, OSError) as exc:  # BuildError, ConfigCI preflight, bad path
        print(f"dlp-ctl: build failed: {exc}", file=sys.stderr)
        return 1
    print(f"built policy {result['policy_id']} v{result['version_ex']} "
          f"({result['allow_files']} allow, {result['deny_files']} deny, "
          f"{len(result['hashed'])} hashed)")
    for f in result["hashed"]:
        print(f"  ~ hash-fallback (no PE version-info): {f}")
    for w in result["warnings"]:
        print(f"  ! {w}")
    print(f"  staged: {result['staging_dir']}")
    print("  run `dlp-ctl appcontrol apply` to deploy it.")
    return 0


def _ac_apply(builder, config) -> int:
    try:
        result = builder.apply(config)
    except (RuntimeError, OSError) as exc:
        print(f"dlp-ctl: apply failed: {exc}", file=sys.stderr)
        return 1
    print(f"applied -> {result['applied']}")
    print("  the agent's inbox watcher will validate + deploy it; "
          "check `dlp-ctl appcontrol status`.")
    return 0


def _ac_status(builder, paths, config) -> int:
    # Local (offline) view first — works even when the agent is down.
    allow_n = len(builder.read_entries(paths.allow_list_path(config)))
    deny_n = len(builder.read_entries(paths.deny_list_path(config)))
    staged = paths.staging_dir(config) / "build" / "manifest.json"
    staged_ver = None
    if staged.is_file():
        try:
            staged_ver = json.loads(staged.read_text(encoding="utf-8")).get("version_ex")
        except (OSError, ValueError):
            staged_ver = "?"
    print(f"lists        : allow={allow_n}, deny={deny_n}")
    print("staged build : " + (f"v{staged_ver} (run `apply` to deploy)"
                                if staged_ver else "(none)"))
    # Live view via the admin-pipe (reuses the existing status command, decision D2).
    resp = _admin_call(config.admin_pipe, {"cmd": "status"})
    if resp is None:
        return 1  # _admin_call already explained why (agent down / access denied)
    if not resp.get("ok"):
        print(f"dlp-ctl: {resp.get('error', 'status failed')}", file=sys.stderr)
        return 1
    ac = resp.get("app_control")
    if not isinstance(ac, dict):
        print("app-control  : (no data)")
        return 0
    _print_appcontrol(ac)
    return 0


def _print_appcontrol(ac: dict) -> None:
    if not ac.get("enabled", False):
        print("app-control  : disabled")
        return
    if not ac.get("running", False):
        print("app-control  : enabled (channel not running)")
        return
    guid = ac.get("policy_guid")
    if not guid:
        print(f"app-control  : no policy deployed "
              f"(pending={ac.get('pending_inbox', 0)}, "
              f"rejected={ac.get('rejected_count', 0)})")
    else:
        blocks = ac.get("blocks") or {}
        print(f"app-control  : policy={guid} v{ac.get('version_ex')} "
              f"(deployed {ac.get('deployed_at')})")
        print(f"  blocks     : {blocks.get('enforce', 0)} enforce / "
              f"{blocks.get('audit', 0)} audit"
              + (f"  last={ac['last_block_at']}" if ac.get("last_block_at") else ""))
        print(f"  inbox      : pending={ac.get('pending_inbox', 0)}, "
              f"rejected={ac.get('rejected_count', 0)}")
    print(f"  forwarder  : {'on' if ac.get('forwarder') else 'off'}")
    if ac.get("last_error"):
        print(f"  last_error : {ac['last_error']}")


def _ac_disable(builder, config, force_local: bool) -> int:
    if force_local:
        try:
            result = builder.disable_local(config)
        except (RuntimeError, OSError) as exc:
            print(f"dlp-ctl: force-local disable failed: {exc}", file=sys.stderr)
            return 1
    else:
        resp = _admin_call(config.admin_pipe, {"cmd": "appcontrol_disable"})
        if resp is None:
            print("  the agent may be down — retry with `--force-local`.", file=sys.stderr)
            return 1
        if not resp.get("ok"):
            print(f"dlp-ctl: {resp.get('error', 'disable failed')}", file=sys.stderr)
            return 1
        result = resp
    if result.get("removed"):
        print("app-control: policy removed.")
        return 0
    err = result.get("last_error") or result.get("error")
    print("dlp-ctl: disable did not remove the policy"
          + (f": {err}" if err else "."), file=sys.stderr)
    return 1


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

    # Phase AC-4: App Control (WDAC) authoring. allow/deny/build/apply are offline
    # local-file ops (run elevated); status/disable talk to the agent's admin-pipe
    # (disable has a --force-local escape hatch for when the agent is dead).
    p_ac = sub.add_parser("appcontrol", parents=[common],
                          help="App Control (WDAC): allow/deny/build/apply/status/disable.")
    ac_sub = p_ac.add_subparsers(dest="ac_command", required=True)
    for verb in ("allow", "deny"):
        p = ac_sub.add_parser(verb, parents=[common],
                              help=f"Add (or --remove) {verb.capitalize()} targets (files/folders).")
        p.add_argument("paths", nargs="+", help="File and/or folder paths.")
        p.add_argument("--remove", action="store_true",
                       help="Remove the given paths from the list instead of adding.")
    p_build = ac_sub.add_parser("build", parents=[common],
                                help="Build + compile a policy from the lists into staging.")
    p_build.add_argument("--version", help="Override the auto-bumped VersionEx (4 dotted ints).")
    ac_sub.add_parser("apply", parents=[common],
                      help="Move the staged build into the inbox (deploy it).")
    ac_sub.add_parser("status", parents=[common],
                      help="Show the App Control channel + local staging/list status.")
    p_disable = ac_sub.add_parser("disable", parents=[common],
                                  help="Remove the deployed policy.")
    p_disable.add_argument("--force-local", action="store_true",
                           help="Drive citool directly (no running agent needed) — escape hatch.")

    args = parser.parse_args(argv)

    config = load_config(getattr(args, "config", None))

    if args.command == "status":
        return _cmd_status(config.admin_pipe)
    if args.command == "reload":
        return _cmd_reload(config.admin_pipe)
    if args.command == "tail":
        return _cmd_tail(_resolve_log_dir(config), args.log, args.n, args.follow)
    if args.command == "appcontrol":
        return _cmd_appcontrol(args, config)
    parser.error("no command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
