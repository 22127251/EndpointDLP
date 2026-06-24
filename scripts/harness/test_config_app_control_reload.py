"""§7.4 — App Control channel hot-reload (poll_seconds / forward_block_events).

These two fields are hot-reloadable via AppControlChannel.apply_config — a DIFFERENT
mechanism than OrchestratorConfig.apply_hot_reload (Layer A asserts apply_hot_reload
leaves them frozen on the config object). The channel needs LocalSystem to START
(deploy to System32\\CodeIntegrity, EvtSubscribe the CI log), so it is excluded from
the orchestrator subprocess harness. Here we construct the channel object WITHOUT
start() and drive apply_config directly; the poll_seconds path is exercised with a
stub watcher so no privilege or threads are needed.
"""
from __future__ import annotations

from orchestrator.app_control.channel import AppControlChannel
from orchestrator.config import _config_from_raw

_RAW = {
    "data_pipe": r"\\.\pipe\d",
    "ctl_pipe": r"\\.\pipe\c",
    "app_control": {
        "enabled": True, "poll_seconds": 3,
        "reconcile_interval_seconds": 30, "forward_block_events": True,
    },
}


def _channel() -> AppControlChannel:
    # Construction resolves dirs + parses the packaged base.xml (cheap, no I/O,
    # no threads). start() is intentionally NOT called.
    return AppControlChannel(_config_from_raw(_RAW))


def test_forward_block_events_reloads():
    ch = _channel()
    assert ch._forward is True
    ch.apply_config({"app_control": {"forward_block_events": False}})
    assert ch._forward is False
    ch.apply_config({"app_control": {"forward_block_events": True}})
    assert ch._forward is True


class _StubWatcher:
    """Stands in for the running InboxWatcher (poll_seconds only updates when a
    watcher exists, i.e. after start())."""

    def __init__(self) -> None:
        self.poll = None

    def set_poll_seconds(self, value) -> None:
        self.poll = value


def test_poll_seconds_reloads():
    ch = _channel()
    ch._watcher = _StubWatcher()
    assert ch._poll_seconds == 3
    ch.apply_config({"app_control": {"poll_seconds": 9}})
    assert ch._poll_seconds == 9
    assert ch._watcher.poll == 9
