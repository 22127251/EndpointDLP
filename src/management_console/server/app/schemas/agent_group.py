from pydantic import BaseModel
from uuid import UUID
from app.schemas.agent import AgentResponse
from app.schemas.policy import PolicyResponse
class AgentGroupCreate(BaseModel):
    name: str
    description: str | None = None


class AgentGroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class AddMembersRequest(BaseModel):
    agent_ids: list[UUID]


class AgentGroupResponse(BaseModel):
    id: UUID
    description: str | None = None
    name: str
    member_count: int = 0
    agents: list[AgentResponse] = []
    policies: list[PolicyResponse] = []
    model_config = {"from_attributes": True}