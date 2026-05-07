from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from app.schemas.policy import PolicyAction, PolicyChannel

class ViolationLogCreate(BaseModel):
    agent_id: UUID
    policy_id: UUID
    channel: PolicyChannel
    action: PolicyAction
    details: dict = Field(
        ...,
        min_length=1,
        max_length=500,
        example={
          "file_path": "/path/to/example.txt", "file_name" : "example.txt", "matched_content": "sensitive data"
        }
    )


class ViolationLogResponse(ViolationLogCreate):
    id: UUID
    agent_id: UUID
    policy_id: UUID
    channel: PolicyChannel
    action: PolicyAction
    details: dict
    created_at: datetime
    model_config = {"from_attributes": True}