from pydantic import BaseModel
from uuid import UUID
from datetime import datetime
from app.models.agent import AgentStatus
from app.schemas.policy import PolicyResponse

class AgentUpdate(BaseModel):
    hostname: str | None = None
    status: AgentStatus | None = None
    group_id: UUID | None = None
    description: str | None = None
class AgentCreate(BaseModel):
    hostname: str
    status: AgentStatus
    description: str | None = None
class AgentResponse(BaseModel):
    id: UUID
    description: str | None = None
    hostname: str
    status: AgentStatus
    last_seen: datetime | None = None
    updated_at: datetime | None = None
    created_at: datetime | None = None
    policies: list[PolicyResponse] = []
    group_id: UUID | None = None
    model_config = {"from_attributes": True}
    


    
