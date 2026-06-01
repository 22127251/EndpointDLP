"""Phase B ctl-pipe protocol tests.

(a) Subscribe with snapshot_request returns a config_snapshot matching the
    spawned orchestrator's config.yaml.
(b) Saving the yaml with ONE non-hot-reloadable field (data_pipe) AND TWO
    hot-reloadable fields (browser.fail_behavior + clipboard.pipe_timeout_ms)
    in a single atomic save results in:
      - a single config_update arriving to each subscriber within ~1.5 s,
      - the data_pipe field overridden back to the in-use value,
      - the other two fields propagated with their new values.
"""
from __future__ import annotations

import json
import os
import time

import pywintypes
import win32event
import win32file
import win32pipe
import yaml

_ERROR_IO_PENDING = 997
_ERROR_BROKEN_PIPE = 109
_ERROR_PIPE_BUSY = 231


def _open_ctl(pipe_name: str, timeout_s: float = 10.0):
    """Open the ctl-pipe with retry — ready timing on this pipe isn't covered
    by conftest's data-pipe wait. Returns an overlapped handle in message mode."""
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"could not open ctl pipe {pipe_name}")
        try:
            win32pipe.WaitNamedPipe(pipe_name, max(1, int(min(500, remaining * 1000))))
        except pywintypes.error:
            time.sleep(0.05)
            continue
        try:
            handle = win32file.CreateFile(
                pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None,
                win32file.OPEN_EXISTING,
                win32file.FILE_FLAG_OVERLAPPED,
                None,
            )
        except pywintypes.error as exc:
            if exc.winerror == _ERROR_PIPE_BUSY:
                continue
            raise
        win32pipe.SetNamedPipeHandleState(
            handle, win32pipe.PIPE_READMODE_MESSAGE, None, None,
        )
        return handle


def _write_msg(handle, msg: dict, timeout_s: float = 2.0) -> None:
    data = json.dumps(msg).encode("utf-8")
    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
    try:
        try:
            win32file.WriteFile(handle, data, overlapped)
        except pywintypes.error as exc:
            if exc.winerror != _ERROR_IO_PENDING:
                raise
        rc = win32event.WaitForSingleObject(overlapped.hEvent, int(timeout_s * 1000))
        if rc == win32event.WAIT_TIMEOUT:
            try:
                win32file.CancelIo(handle)
            except pywintypes.error:
                pass
            raise TimeoutError("ctl write timed out")
        win32file.GetOverlappedResult(handle, overlapped, False)
    finally:
        win32file.CloseHandle(overlapped.hEvent)


def _read_msg(handle, timeout_s: float = 2.0) -> dict:
    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
    try:
        buf = win32file.AllocateReadBuffer(64 * 1024)
        try:
            win32file.ReadFile(handle, buf, overlapped)
        except pywintypes.error as exc:
            if exc.winerror == _ERROR_BROKEN_PIPE:
                raise OSError("ctl pipe broken") from exc
            if exc.winerror != _ERROR_IO_PENDING:
                raise
        rc = win32event.WaitForSingleObject(overlapped.hEvent, int(timeout_s * 1000))
        if rc == win32event.WAIT_TIMEOUT:
            try:
                win32file.CancelIo(handle)
            except pywintypes.error:
                pass
            raise TimeoutError("ctl read timed out")
        n = win32file.GetOverlappedResult(handle, overlapped, False)
        return json.loads(bytes(buf[:n]).decode("utf-8"))
    finally:
        win32file.CloseHandle(overlapped.hEvent)


def _subscribe(pipe_name: str, component: str) -> tuple[object, dict]:
    """Open ctl, send subscribe+snapshot_request, return (handle, snapshot_msg)."""
    handle = _open_ctl(pipe_name)
    try:
        _write_msg(handle, {
            "type": "subscribe",
            "component": component,
            "pid": os.getpid(),
            "snapshot_request": True,
        })
        msg = _read_msg(handle)
    except BaseException:
        win32file.CloseHandle(handle)
        raise
    assert msg["type"] == "config_snapshot", f"expected config_snapshot, got {msg}"
    return handle, msg


def _atomic_write_yaml(path, raw: dict) -> None:
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------


def test_subscribe_returns_snapshot(make_orchestrator):
    orch = make_orchestrator()
    raw = yaml.safe_load(orch.config_path.read_text(encoding="utf-8"))
    ctl_pipe = raw["ctl_pipe"]

    handle, snap = _subscribe(ctl_pipe, "browser")
    try:
        assert snap["section"] == "browser"
        config = snap["config"]
        # Top-level pipe names are included so clients can bootstrap.
        assert config["data_pipe"] == orch.pipe_name
        assert config["ctl_pipe"] == ctl_pipe
        # Browser section reflects what's on disk.
        assert config["browser"]["pipe_timeout_seconds"] == raw["browser"]["pipe_timeout_seconds"]
        assert config["browser"]["fail_behavior"] == raw["browser"]["fail_behavior"]
    finally:
        win32file.CloseHandle(handle)


def test_yaml_save_selective_skip_and_propagate(make_orchestrator):
    orch = make_orchestrator()
    raw = yaml.safe_load(orch.config_path.read_text(encoding="utf-8"))
    ctl_pipe = raw["ctl_pipe"]
    old_data_pipe = raw["data_pipe"]
    old_fail = raw["browser"]["fail_behavior"]
    old_clip_timeout = raw["clipboard"]["pipe_timeout_ms"]

    browser_handle, browser_snap = _subscribe(ctl_pipe, "browser")
    clipboard_handle, clipboard_snap = _subscribe(ctl_pipe, "clipboard")

    try:
        # Sanity-check snapshots match starting state.
        assert browser_snap["config"]["browser"]["fail_behavior"] == old_fail
        assert clipboard_snap["config"]["clipboard"]["pipe_timeout_ms"] == old_clip_timeout

        # Mutate one non-hot-reloadable + two hot-reloadable fields in one save.
        new_fail = "allow" if old_fail == "block" else "block"
        new_clip_timeout = old_clip_timeout + 1000
        raw["data_pipe"] = r"\\.\pipe\dlp_changed_should_not_apply"
        raw["browser"]["fail_behavior"] = new_fail
        raw["clipboard"]["pipe_timeout_ms"] = new_clip_timeout
        _atomic_write_yaml(orch.config_path, raw)

        browser_update = _read_msg(browser_handle, timeout_s=3.0)
        clipboard_update = _read_msg(clipboard_handle, timeout_s=3.0)

        assert browser_update["type"] == "config_update", browser_update
        assert clipboard_update["type"] == "config_update", clipboard_update

        # data_pipe overridden back to in-use value in BOTH payloads.
        assert browser_update["config"]["data_pipe"] == old_data_pipe, browser_update
        assert clipboard_update["config"]["data_pipe"] == old_data_pipe, clipboard_update

        # Hot-reloadable changes propagated.
        assert browser_update["config"]["browser"]["fail_behavior"] == new_fail, browser_update
        assert clipboard_update["config"]["clipboard"]["pipe_timeout_ms"] == new_clip_timeout, clipboard_update
    finally:
        win32file.CloseHandle(browser_handle)
        win32file.CloseHandle(clipboard_handle)
