from enum import StrEnum
from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime



class PolicyAction(StrEnum):
    BLOCK = "block"
    ALERT = "alert"
    ALLOW = "allow"

class PolicyChannel(StrEnum):
    ALL = "all"
    EMAIL = "email"
    CLIPBOARD = "clipboard"
    BROWSER = "browser"
    USB = "usb"

class RuleType(StrEnum):
    REGEX = "regex"
    KEYWORD = "keyword"

class PolicyCreate(BaseModel):
    name: str = Field(
        ..., 
        min_length=1, max_length=255, 
        examples=["Sensitive Data Policy"]
    )
    description: str | None = None
    rule_type: RuleType
    rule: dict = Field(
        ...,
        min_length=1,
        max_length=500,
        examples=[
            {
                "pattern": "\\b\\d{16}\\b",
                "description": "Detect credit card numbers"
            }
        ]
    )
    action: PolicyAction
    channel: PolicyChannel
    is_active: bool = True


class PolicyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    rule_type: RuleType | None = None
    rule: dict | None = None
    action: PolicyAction | None = None
    channel: PolicyChannel | None = None
    is_active: bool | None = None


class PolicyResponse(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    rule_type: RuleType
    rule: dict
    action: PolicyAction
    channel: PolicyChannel
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
