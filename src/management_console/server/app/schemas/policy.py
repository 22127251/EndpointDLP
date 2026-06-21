from enum import StrEnum
from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime


class PolicyAction(StrEnum):
    BLOCK = "block"
    ALLOW_LOG = "allow_log"
    ALLOW = "allow"


class PolicyChannel(StrEnum):
    BROWSER = "browser"
    CLIPBOARD = "clipboard"
    PERIPHERAL_STORAGE = "peripheral_storage"


class RuleType(StrEnum):
    REGEX = "regex"
    KEYWORD = "keyword"
    DENYLIST = "denylist"


class PolicyCreate(BaseModel):
    name: str = Field(
        ...,
        min_length=1, max_length=255,
        examples=["Block Visa Card"]
    )
    description: str | None = None
    type: RuleType = RuleType.REGEX
    patterns: list[str] | None = None
    keywords: list[str] | None = None
    channels: list[PolicyChannel] = [PolicyChannel.BROWSER, PolicyChannel.CLIPBOARD, PolicyChannel.PERIPHERAL_STORAGE]
    action: PolicyAction = PolicyAction.BLOCK
    context_words: list[str] = []
    context_range: int = 0
    is_active: bool = True


class PolicyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    type: RuleType | None = None
    patterns: list[str] | None = None
    keywords: list[str] | None = None
    channels: list[PolicyChannel] | None = None
    action: PolicyAction | None = None
    context_words: list[str] | None = None
    context_range: int | None = None
    is_active: bool | None = None


class AgentRef(BaseModel):
    """Minimal agent reference for policy assignment display."""
    id: UUID
    hostname: str
    status: str | None = None
    model_config = {"from_attributes": True}


class AgentGroupRef(BaseModel):
    """Minimal agent group reference for policy assignment display."""
    id: UUID
    name: str
    model_config = {"from_attributes": True}


class PolicyResponse(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    type: RuleType
    patterns: list[str] | None = None
    keywords: list[str] | None = None
    channels: list[PolicyChannel]
    action: PolicyAction
    context_words: list[str] = []
    context_range: int = 0
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PolicyDetailResponse(PolicyResponse):
    individual_agents: list[AgentRef] = []
    agent_groups: list[AgentGroupRef] = []
