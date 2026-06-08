"""Phase F: dispatcher event-log emission, client-tuple invariance, in-flight,
and the bounded drain. Pure unit tests with a stub PolicyManager (no DLPEngine).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from orchestrator.dispatcher import Dispatcher


def _cfg():
    return SimpleNamespace(
        clipboard_workers=2, browser_workers=2, peripheral_storage_workers=2)


class _Violation:
    def __init__(self, policy_id: str) -> None:
        self.policy_id = policy_id
        self.matches = ["m"]


class _StubPM:
    """analyze() returns a fixed (decision, violations) tuple."""
    def __init__(self, result) -> None:
        self._result = result

    def analyze(self, channel, kind, text=None, file_path=None, req_id=""):
        return self._result


@pytest.fixture
def events_capture():
    """Capture lines emitted to the dlp.events logger."""
    logger = logging.getLogger("dlp.events")
    lines: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = _Cap()
    prev_level = logger.level
    prev_propagate = logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.propagate = prev_propagate


def test_browser_allow_event(events_capture):
    disp = Dispatcher(_cfg(), _StubPM(("ALLOW", [])))
    req = {
        "channel": "browser", "kind": "file", "file_path": r"C:\x\cccd.pdf",
        "metadata": {"url": "https://drive.google.com/u", "filename": "cccd.pdf"},
        "req_id": "r1",
    }
    assert disp.analyze(req) == ("ALLOW", True, "")
    assert len(events_capture) == 1
    rec = json.loads(events_capture[0])
    assert rec["channel"] == "browser"
    assert rec["kind"] == "file"
    assert rec["decision"] == "ALLOW"
    assert rec["violations"] == []
    assert rec["name"] == "cccd.pdf"
    assert rec["url"] == "https://drive.google.com/u"
    assert rec["superseded"] is False
    assert isinstance(rec["elapsed_ms"], (int, float))


def test_browser_block_event_has_violation_ids(events_capture):
    disp = Dispatcher(_cfg(), _StubPM(("BLOCK", [_Violation("block_visa_browser")])))
    req = {"channel": "browser", "kind": "file", "file_path": r"C:\x\f.pdf",
           "metadata": {"filename": "f.pdf"}, "req_id": "r2"}
    decision, write, reason = disp.analyze(req)
    assert decision == "BLOCK"
    assert write is True
    assert reason.startswith("Sensitive data detected")
    rec = json.loads(events_capture[0])
    assert rec["decision"] == "BLOCK"
    # violations are {policy_id, count} objects; _Violation has matches=["m"] → 1.
    assert rec["violations"] == [{"policy_id": "block_visa_browser", "count": 1}]


def test_browser_url_query_stripped(events_capture):
    disp = Dispatcher(_cfg(), _StubPM(("BLOCK", [_Violation("block_x")])))
    long_url = ("https://clients6.google.com/upload/drive/v2internal/files"
                "?openDrive=false&reason=202&uploadType=multipart&key=AIzaSyD_secret")
    req = {"channel": "browser", "kind": "file", "file_path": r"C:\x\f.csv",
           "metadata": {"filename": "f.csv", "url": long_url}, "req_id": "u1"}
    disp.analyze(req)
    rec = json.loads(events_capture[0])
    assert rec["url"] == "https://clients6.google.com/upload/drive/v2internal/files"
    assert "?" not in rec["url"] and "key=" not in rec["url"]


def test_peripheral_block_keeps_empty_client_reason(events_capture):
    # Peripheral never sent a reason to the client; that must be preserved, but
    # the event still records the violation ids.
    disp = Dispatcher(_cfg(), _StubPM(("BLOCK", [_Violation("block_cccd")])))
    req = {"channel": "peripheral_storage", "kind": "file",
           "file_path": r"C:\x\id.docx", "req_id": "r3"}
    assert disp.analyze(req) == ("BLOCK", True, "")
    rec = json.loads(events_capture[0])
    assert rec["channel"] == "peripheral_storage"
    assert rec["violations"] == [{"policy_id": "block_cccd", "count": 1}]
    assert rec["name"] == "id.docx"


def test_clipboard_basic_event(events_capture):
    disp = Dispatcher(_cfg(), _StubPM(("ALLOW", [])))
    req = {"channel": "clipboard", "kind": "text", "text": "hi", "req_id": "r4"}
    assert disp.analyze(req) == ("ALLOW", True, "")
    rec = json.loads(events_capture[0])
    assert rec["channel"] == "clipboard"
    assert rec["kind"] == "text"
    assert rec["superseded"] is False
    assert "name" not in rec  # no filename / file_path for clipboard text


class _GatedClipPM:
    """First analyze() blocks on a gate; later calls return immediately."""
    def __init__(self) -> None:
        self.calls = 0
        self.gate = threading.Event()
        self.first_started = threading.Event()
        self._lock = threading.Lock()

    def analyze(self, channel, kind, text=None, file_path=None, req_id=""):
        with self._lock:
            n = self.calls
            self.calls += 1
        if n == 0:
            self.first_started.set()
            self.gate.wait(timeout=5)
        return ("ALLOW", [])


def test_clipboard_supersession_event(events_capture):
    pm = _GatedClipPM()
    disp = Dispatcher(_cfg(), pm)
    results: dict[str, tuple] = {}

    def _run_first():
        results["first"] = disp.analyze(
            {"channel": "clipboard", "kind": "text", "text": "a", "req_id": "first"})

    t = threading.Thread(target=_run_first)
    t.start()
    assert pm.first_started.wait(timeout=5)
    # Second request supersedes the first.
    res2 = disp.analyze({"channel": "clipboard", "kind": "text", "text": "b", "req_id": "second"})
    pm.gate.set()
    t.join(timeout=5)

    assert res2 == ("ALLOW", True, "")
    # The first (superseded) request must signal write_response=False.
    assert results["first"] == ("ALLOW", False, "")
    by_req = {json.loads(l)["req_id"]: json.loads(l) for l in events_capture}
    assert by_req["first"]["superseded"] is True
    assert by_req["second"]["superseded"] is False


def test_inflight_counts_and_drain():
    gate = threading.Event()

    def _slow(*a, **k):
        gate.wait(timeout=5)
        return ("ALLOW", [])

    disp = Dispatcher(_cfg(), SimpleNamespace(analyze=_slow))
    assert disp.inflight_counts() == {
        "clipboard": 0, "browser": 0, "peripheral_storage": 0}

    disp._tracked_submit("peripheral_storage", disp._peripheral_pool,
                         "peripheral_storage", "file")
    assert disp.inflight_counts()["peripheral_storage"] == 1

    t0 = time.monotonic()
    abandoned = disp.drain(0.5)
    elapsed = time.monotonic() - t0
    assert abandoned == 1          # the blocked analysis is abandoned at the deadline
    assert elapsed < 2.0           # drain honored the timeout
    gate.set()                     # let the worker finish so the process can exit
