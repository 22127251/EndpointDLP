# app/schemas/policy.py
from enum import StrEnum

from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from typing import Literal
from enum import StrEnum


class PolicyAction(StrEnum):
    BLOCK = "block"
    ALERT = "alert"
    ALLOW = "allow"

class PolicyChanel(StrEnum):
    ALL = "all"
    EMAIL = "email"
    CLIPBOARD = "clipboard"
    BROWSER = "browser"
    USB = "usb"

class RuleType(StrEnum):
    REGEX = "regex"

class PolicyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, examples=["Block copying Identification ID to USB"])
    description: str | None = None
    rule_type: RuleType
    rule: dict
    action: PolicyAction
    channel: PolicyChanel
    is_active: bool = True


class PolicyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    rule_type: RuleType | None = None
    rule: dict | None = None
    action: PolicyAction | None = None
    channel: PolicyChanel | None = None
    is_active: bool | None = None


class PolicyResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    rule_type: RuleType
    rule: dict
    action: PolicyAction
    channel: PolicyChanel
    is_active: bool
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
