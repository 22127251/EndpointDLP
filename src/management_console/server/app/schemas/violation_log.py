from pydantic import BaseModel
from uuid import UUID
from datetime import datetime


class ViolationPolicyMatchCreate(BaseModel):
    """One triggered policy in a violation event (mirrors an events.jsonl
    ``violations`` entry)."""
    policy_id: UUID | None = None
    action: str = "block"
    count: int = 0
    with_context: int = 0
    context_words_triggered: list[str] = []


class ViolationLogCreate(BaseModel):
    """One notable agent decision. Recorded iff it has matches or a ``reason``."""
    agent_id: UUID
    channel: str = "all"
    decision: str = "BLOCK"           # BLOCK | ALLOW
    reason: str | None = None
    details: dict = {}                # {req_id, name, url, elapsed_ms}
    matches: list[ViolationPolicyMatchCreate] = []


class ViolationPolicyMatchResponse(BaseModel):
    id: UUID
    policy_id: UUID | None = None
    policy_name: str | None = None
    action: str
    count: int = 0
    with_context: int = 0
    context_words_triggered: list[str] = []
    model_config = {"from_attributes": True}


class ViolationLogResponse(BaseModel):
    id: UUID
    agent_id: UUID
    agent_hostname: str | None = None
    channel: str
    decision: str
    reason: str | None = None
    details: dict = {}
    matches: list[ViolationPolicyMatchResponse] = []
    created_at: datetime
    model_config = {"from_attributes": True}
