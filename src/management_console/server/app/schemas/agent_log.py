from pydantic import BaseModel
from uuid import UUID
from datetime import datetime


class AgentLogPush(BaseModel):
    events_tail: str = ""
    agent_log_tail: str = ""


class AgentLogResponse(BaseModel):
    id: UUID
    agent_id: UUID
    log_type: str
    content: str
    byte_offset: int
    created_at: datetime

    model_config = {"from_attributes": True}
