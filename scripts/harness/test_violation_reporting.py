"""Phase 1: faithful event-centric violation reporting.

Covers the agent half of the new contract:
- the dispatcher's violation callback fires for any NOTABLE decision (a policy match
  OR a failure `reason`) and never for a clean ALLOW;
- the payload is the event shape {channel, decision, reason, details, matches} with the
  renamed `context_words_triggered` field and one match per triggered policy (count is
  a field, never a row multiplier);
- CloudBridge._violation_worker fills agent_id and coerces non-UUID policy ids to null.

Pure in-process unit tests (stub PolicyManager, stubbed HTTP).
"""
from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import yaml

from analyzer.policy import load_policies
from orchestrator.cloud_bridge import (
    CloudBridge, translate_policies, _strip_control_chars, _valid_uuid_or_none,
)
from orchestrator.dispatcher import Dispatcher


# ── dispatcher callback shape + gating ────────────────────────────────────────

def _cfg():
    return SimpleNamespace(
        clipboard_workers=2, browser_workers=2, peripheral_storage_workers=2)


class _Violation:
    def __init__(self, policy_id, action="block", context_words=None,
                 user_message="", matches=None):
        self.policy_id = policy_id
        self.action = action
        self.context_words = context_words or []
        self.matches = matches if matches is not None else ["m"]
        self.user_message = user_message


class _StubPM:
    def __init__(self, result):
        self._result = result if len(result) == 3 else (*result, None)

    def analyze(self, channel, kind, text=None, file_path=None, req_id=""):
        return self._result


def _disp_with_callback(pm_result):
    disp = Dispatcher(_cfg(), _StubPM(pm_result))
    events: list[dict] = []
    disp.set_violation_callback(events.append)
    return disp, events


def _browser_req(req_id="r"):
    return {"channel": "browser", "kind": "file", "file_path": r"C:\x\f.csv",
            "metadata": {"filename": "f.csv", "url": "https://drive.google.com/u"},
            "req_id": req_id}


def test_callback_policy_block_one_match_per_policy():
    disp, events = _disp_with_callback(("BLOCK", [
        _Violation("uuid-A", action="block", context_words=["visa"],
                   matches=[SimpleNamespace(has_context=True),
                            SimpleNamespace(has_context=False)])]))
    disp.analyze(_browser_req("r1"))

    assert len(events) == 1
    ev = events[0]
    assert ev["channel"] == "browser"
    assert ev["decision"] == "BLOCK"
    assert ev["reason"] == "policy_violation"
    assert ev["details"]["req_id"] == "r1"
    assert ev["details"]["name"] == "f.csv"
    # ONE match for the policy; count is a field (2 matches), not two rows.
    assert ev["matches"] == [
        {"policy_id": "uuid-A", "action": "block", "count": 2,
         "with_context": 1, "context_words_triggered": ["visa"]}]


def test_callback_allow_log_is_reported():
    disp, events = _disp_with_callback(("ALLOW", [
        _Violation("uuid-B", action="allow_log")]))
    disp.analyze(_browser_req("r2"))

    assert len(events) == 1
    ev = events[0]
    assert ev["decision"] == "ALLOW"
    assert ev["reason"] is None
    assert ev["matches"][0]["action"] == "allow_log"


def test_callback_fail_open_allow_is_reported():
    # fail_open: a failure category with an ALLOW verdict — events.jsonl logs it, so
    # the server must too (the old `violations or decision==BLOCK` gate dropped it).
    disp, events = _disp_with_callback(("ALLOW", [], "oversize"))
    disp.analyze(_browser_req("r3"))

    assert len(events) == 1
    ev = events[0]
    assert ev["decision"] == "ALLOW"
    assert ev["reason"] == "oversize"
    assert ev["matches"] == []


def test_callback_fail_closed_block_is_reported():
    disp, events = _disp_with_callback(("BLOCK", [], "timeout"))
    disp.analyze(_browser_req("r4"))

    assert len(events) == 1
    assert events[0]["decision"] == "BLOCK"
    assert events[0]["reason"] == "timeout"
    assert events[0]["matches"] == []


def test_callback_clean_allow_not_reported():
    disp, events = _disp_with_callback(("ALLOW", []))
    disp.analyze(_browser_req("r5"))
    assert events == []  # no match, no reason → nothing to audit


# ── worker: fills agent_id, sanitizes policy_id ───────────────────────────────

def _bridge(agent_id="agent-uuid"):
    cfg = SimpleNamespace(
        server_url="http://192.168.6.1:8000", server_agent_id=agent_id,
        server_heartbeat_interval=30, server_log_sync_interval=300,
        server_enabled=True, policies_file="analyzer/policies.yaml")
    b = CloudBridge(cfg)
    b._agent_id = agent_id
    return b


def test_worker_fills_agent_id_and_sanitizes_policy_id():
    bridge = _bridge("0a000000-0000-7000-8000-000000000001")
    posted: list[tuple] = []

    def fake_post(path, body, timeout=5):
        posted.append((path, body))
        return (201, {"status": "received"})

    bridge._post = fake_post
    bridge._violation_queue = queue.Queue()

    valid = "01234567-89ab-cdef-0123-456789abcdef"
    bridge._violation_queue.put({
        "channel": "browser", "decision": "BLOCK", "reason": "policy_violation",
        "details": {"req_id": "r1"},
        "matches": [
            {"policy_id": valid, "action": "block", "count": 1,
             "with_context": 0, "context_words_triggered": []},
            {"policy_id": "not-a-uuid", "action": "allow_log", "count": 2,
             "with_context": 0, "context_words_triggered": []},
        ],
    })

    t = threading.Thread(target=bridge._violation_worker, daemon=True)
    t.start()
    for _ in range(100):
        if posted:
            break
        time.sleep(0.02)
    bridge.stop()
    t.join(timeout=3)

    assert len(posted) == 1
    path, body = posted[0]
    assert path == "/api/v1/violation-logs/"
    assert body["agent_id"] == "0a000000-0000-7000-8000-000000000001"
    assert body["matches"][0]["policy_id"] == valid
    assert body["matches"][1]["policy_id"] is None   # bogus id coerced to null


def test_valid_uuid_or_none():
    assert _valid_uuid_or_none("01234567-89ab-cdef-0123-456789abcdef") == \
        "01234567-89ab-cdef-0123-456789abcdef"
    assert _valid_uuid_or_none("not-a-uuid") is None
    assert _valid_uuid_or_none("") is None
    assert _valid_uuid_or_none(None) is None


# ── translate_policies: ladder + scores + user_message + keyword + strip ──────

def test_translate_policies_builds_real_ladder(tmp_path):
    server = [{
        "id": "01234567-89ab-cdef-0123-456789abcdef", "name": "Visa",
        "is_active": True, "type": "regex", "patterns": [r"\b4\d{3}\b"],
        "context_words": ["visa"], "context_range": 120,
        "user_message": "Credit card number (Visa) detected",
        "score_base": 0.5, "score_context_boost": 0.5,
        "actions": [{"min_score": 1.0, "action": "block"},
                    {"min_score": 0.0, "action": "allow_log"}],
    }]
    local = translate_policies(server)
    p = local["policies"][0]
    assert "action" not in p                      # the dead single field is gone
    assert p["user_message"] == "Credit card number (Visa) detected"
    assert p["score_base"] == 0.5 and p["score_context_boost"] == 0.5
    assert p["actions"][0] == {"min_score": 1.0, "action": "block"}

    # round-trip through YAML + the analyzer loader → a real enforcing ladder.
    f = tmp_path / "policies.yaml"
    f.write_text(yaml.dump(local, allow_unicode=True), encoding="utf-8")
    pol = load_policies(f)[0]
    assert pol.resolve_action(1.0) == "block"      # format + context
    assert pol.resolve_action(0.5) == "allow_log"  # format only
    assert pol.user_message == "Credit card number (Visa) detected"
    assert pol.patterns == [r"\b4\d{3}\b"]          # backslashes survive byte-clean


def test_translate_keyword_maps_to_denylist():
    local = translate_policies([{
        "id": "x", "name": "Secrets", "is_active": True, "type": "keyword",
        "keywords": ["secret"], "actions": [{"min_score": 1.0, "action": "block"}]}])
    p = local["policies"][0]
    assert p["type"] == "denylist"
    assert p["keywords"] == ["secret"]


def test_translate_strips_control_chars():
    # "\b" here is a literal backspace (0x08) — the create-time corruption shape.
    local = translate_policies([{
        "id": "x", "name": "Bad", "is_active": True, "type": "regex",
        "patterns": ["\b4\\d"], "actions": []}])
    assert "\x08" not in local["policies"][0]["patterns"][0]


def test_strip_control_chars_keeps_legit_escapes():
    assert _strip_control_chars([r"\b4\d", "visa"]) == [r"\b4\d", "visa"]
    assert _strip_control_chars(["a\x08b"]) == ["ab"]
    assert _strip_control_chars(None) == []
