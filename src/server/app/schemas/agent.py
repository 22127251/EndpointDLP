# app/schemas/agent.py
from pydantic import BaseModel
from uuid import UUID
from datetime import datetime

class AgentResponse(BaseModel):
    id: UUID
    status: str
    last_seen: datetime | None
    updated_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
