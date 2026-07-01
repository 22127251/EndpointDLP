"""Phase 1 server tests: event-centric violation logging (parent + children)."""
from __future__ import annotations

from conftest import make_agent, make_policy


async def test_policy_block_creates_parent_and_children(client):
    agent_id = await make_agent(client)
    policy_id = await make_policy(client)

    r = await client.post("/api/v1/violation-logs/", json={
        "agent_id": agent_id, "channel": "browser",
        "decision": "BLOCK", "reason": "policy_violation",
        "details": {"req_id": "r1", "name": "cards.csv"},
        "matches": [{"policy_id": policy_id, "action": "block", "count": 1000,
                     "with_context": 4, "context_words_triggered": ["visa"]}],
    })
    assert r.status_code == 201, r.text
    assert r.json()["matches"] == 1

    lst = await client.get("/api/v1/violation-logs/")
    assert lst.status_code == 200
    item = lst.json()["items"][0]
    assert item["decision"] == "BLOCK"
    assert item["reason"] == "policy_violation"
    assert item["agent_hostname"] == "vm-1"
    assert len(item["matches"]) == 1
    m = item["matches"][0]
    assert m["policy_id"] == policy_id
    assert m["policy_name"] == "Visa"
    assert m["count"] == 1000                         # count is a field, not rows
    assert m["with_context"] == 4
    assert m["context_words_triggered"] == ["visa"]


async def test_allow_log_event_recorded(client):
    agent_id = await make_agent(client)
    policy_id = await make_policy(client, name="Phones")

    r = await client.post("/api/v1/violation-logs/", json={
        "agent_id": agent_id, "channel": "clipboard", "decision": "ALLOW",
        "reason": None,
        "matches": [{"policy_id": policy_id, "action": "allow_log", "count": 5}],
    })
    assert r.status_code == 201, r.text
    item = (await client.get("/api/v1/violation-logs/")).json()["items"][0]
    assert item["decision"] == "ALLOW"
    assert item["reason"] is None
    assert item["matches"][0]["action"] == "allow_log"


async def test_fail_open_allow_filterable_by_reason(client):
    agent_id = await make_agent(client)
    r = await client.post("/api/v1/violation-logs/", json={
        "agent_id": agent_id, "channel": "browser", "decision": "ALLOW",
        "reason": "oversize", "matches": [],
    })
    assert r.status_code == 201, r.text

    # Audit filter by reason returns the fail_open ALLOW (a parent with no children).
    filtered = await client.get("/api/v1/violation-logs/", params={"reason": "oversize"})
    items = filtered.json()["items"]
    assert len(items) == 1
    assert items[0]["decision"] == "ALLOW"
    assert items[0]["reason"] == "oversize"
    assert items[0]["matches"] == []

    # A different reason filter excludes it.
    other = await client.get("/api/v1/violation-logs/", params={"reason": "timeout"})
    assert other.json()["total"] == 0


async def test_fail_closed_block_no_children(client):
    agent_id = await make_agent(client)
    r = await client.post("/api/v1/violation-logs/", json={
        "agent_id": agent_id, "channel": "peripheral_storage", "decision": "BLOCK",
        "reason": "timeout", "matches": [],
    })
    assert r.status_code == 201, r.text
    item = (await client.get("/api/v1/violation-logs/")).json()["items"][0]
    assert item["decision"] == "BLOCK"
    assert item["reason"] == "timeout"
    assert item["matches"] == []


async def test_deleted_policy_match_kept_as_null(client):
    agent_id = await make_agent(client)
    # A match whose policy id doesn't exist on the server is kept with policy_id=null.
    ghost = "01234567-89ab-cdef-0123-456789abcdef"
    r = await client.post("/api/v1/violation-logs/", json={
        "agent_id": agent_id, "channel": "browser", "decision": "BLOCK",
        "reason": "policy_violation",
        "matches": [{"policy_id": ghost, "action": "block", "count": 1}],
    })
    assert r.status_code == 201, r.text
    item = (await client.get("/api/v1/violation-logs/")).json()["items"][0]
    assert item["matches"][0]["policy_id"] is None
    assert item["matches"][0]["action"] == "block"
    assert item["matches"][0]["policy_name"] is None


async def test_missing_agent_404(client):
    r = await client.post("/api/v1/violation-logs/", json={
        "agent_id": "01234567-89ab-cdef-0123-456789abcdef",
        "channel": "browser", "decision": "BLOCK", "reason": "policy_violation",
        "matches": [],
    })
    assert r.status_code == 404, r.text
