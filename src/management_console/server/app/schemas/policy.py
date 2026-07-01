from enum import StrEnum
from pydantic import BaseModel, Field, field_validator
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


class PolicyActionRung(BaseModel):
    """One rung of the action ladder: a match scoring >= ``min_score`` resolves to
    ``action``. Rungs are applied high -> low; a 0.0 rung is the floor."""
    min_score: float = Field(ge=0.0)
    action: PolicyAction


# Canonical new-policy ladder: format + context -> block, format only -> allow_log.
def _default_actions() -> list[PolicyActionRung]:
    return [
        PolicyActionRung(min_score=1.0, action=PolicyAction.BLOCK),
        PolicyActionRung(min_score=0.0, action=PolicyAction.ALLOW_LOG),
    ]


def _reject_control_chars(values: list[str] | None) -> list[str] | None:
    """Reject C0 control characters (``\\x00``-``\\x1f``) in any item. A backspace
    stored in a pattern serialises to JSON ``\\b`` and silently corrupts the agent's
    analyzer (conflict #2). Legitimate regex escapes (``\\b``, ``\\d``, ``\\t``) are
    typed as backslash + letter and are ordinary printable characters, so they pass."""
    if values is None:
        return values
    for v in values:
        if any(ord(c) < 0x20 for c in v):
            raise ValueError(
                "control characters are not allowed in patterns / keywords / context words"
            )
    return values


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
    # End-user-facing block reason (shown to the user; never the policy id).
    user_message: str = ""
    # Confidence scoring + action ladder (the sole action mechanism).
    score_base: float = Field(0.5, ge=0.0)
    score_context_boost: float = Field(0.5, ge=0.0)
    actions: list[PolicyActionRung] = Field(default_factory=_default_actions)
    context_words: list[str] = []
    context_range: int = 0
    is_active: bool = True

    @field_validator("patterns", "keywords", "context_words")
    @classmethod
    def _no_control_chars(cls, v):
        return _reject_control_chars(v)


class PolicyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    type: RuleType | None = None
    patterns: list[str] | None = None
    keywords: list[str] | None = None
    channels: list[PolicyChannel] | None = None
    user_message: str | None = None
    score_base: float | None = Field(None, ge=0.0)
    score_context_boost: float | None = Field(None, ge=0.0)
    actions: list[PolicyActionRung] | None = None
    context_words: list[str] | None = None
    context_range: int | None = None
    is_active: bool | None = None

    @field_validator("patterns", "keywords", "context_words")
    @classmethod
    def _no_control_chars(cls, v):
        return _reject_control_chars(v)


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
    user_message: str | None = ""
    score_base: float = 0.5
    score_context_boost: float = 0.5
    actions: list[PolicyActionRung] = []
    context_words: list[str] = []
    context_range: int = 0
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PolicyDetailResponse(PolicyResponse):
    individual_agents: list[AgentRef] = []
    agent_groups: list[AgentGroupRef] = []
