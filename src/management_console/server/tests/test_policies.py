"""Phase 1 server tests: score-ladder policies + the control-char guard."""
from __future__ import annotations


async def test_create_policy_with_ladder_round_trip(client):
    r = await client.post("/api/v1/policies/", json={
        "name": "Block Visa Card",
        "type": "regex",
        "patterns": [r"\b4\d{3} ?\d{4} ?\d{4} ?\d{4}\b"],
        "user_message": "Credit card number (Visa) detected",
        "score_base": 0.5,
        "score_context_boost": 0.5,
        "actions": [{"min_score": 1.0, "action": "block"},
                    {"min_score": 0.0, "action": "allow_log"}],
        "context_words": ["visa", "credit card"],
        "context_range": 120,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert "action" not in body                       # the dead single field is gone
    assert body["user_message"] == "Credit card number (Visa) detected"
    assert body["score_base"] == 0.5
    assert body["score_context_boost"] == 0.5
    assert body["actions"] == [
        {"min_score": 1.0, "action": "block"},
        {"min_score": 0.0, "action": "allow_log"}]
    assert body["patterns"] == [r"\b4\d{3} ?\d{4} ?\d{4} ?\d{4}\b"]

    # The list endpoint returns the same ladder shape.
    lst = await client.get("/api/v1/policies/")
    assert lst.status_code == 200
    items = lst.json()["items"]
    assert items[0]["actions"][0] == {"min_score": 1.0, "action": "block"}


async def test_create_policy_default_ladder(client):
    r = await client.post("/api/v1/policies/", json={"name": "Defaults", "type": "regex",
                                                     "patterns": ["abc"]})
    assert r.status_code == 201, r.text
    # Canonical default ladder when none is supplied.
    assert r.json()["actions"] == [
        {"min_score": 1.0, "action": "block"},
        {"min_score": 0.0, "action": "allow_log"}]


async def test_control_char_pattern_rejected_422(client):
    # A backspace (0x08) inside a pattern is the create-time corruption shape — reject.
    r = await client.post("/api/v1/policies/", json={
        "name": "Bad", "type": "regex", "patterns": ["\b4\\d"]})
    assert r.status_code == 422, r.text


async def test_legit_regex_escapes_accepted(client):
    # Backslash + letter (\b, \d) are ordinary printable chars and must pass.
    r = await client.post("/api/v1/policies/", json={
        "name": "Good", "type": "regex", "patterns": [r"\b4\d{3}\b"],
        "context_words": [r"\tindent"]})
    assert r.status_code == 201, r.text


async def test_double_backslash_pattern_warns_not_blocks(client, caplog):
    # A double-escaped pattern is the copy-from-YAML mistake — we WARN, not block,
    # so the policy is still created (201) and a warning is logged.
    import logging
    with caplog.at_level(logging.WARNING):
        r = await client.post("/api/v1/policies/", json={
            "name": "DoubleEscaped", "type": "regex",
            "patterns": ["\\\\b\\\\d{12}\\\\b"]})
    assert r.status_code == 201, r.text
    assert "double backslash" in caplog.text
