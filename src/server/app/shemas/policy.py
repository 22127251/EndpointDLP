# app/schemas/policy.py
from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from typing import Literal


# --- Detection Config Examples ---
# Keyword: {"keywords": ["Bảng lương", "Chiến lược kinh doanh", "Bí mật"]}
# Regex: {"patterns": [{"name": "CCCD", "pattern": "\\d{12}"}, {"name": "Phone", "pattern": "0\\d{9}"}]}
# Fingerprint: {"fingerprint_ids": ["uuid1", "uuid2"]}

class PolicyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, examples=["Chặn copy CCCD ra USB"])
    description: str | None = None
    detection_type: Literal["keyword", "regex", "fingerprint"]
    detection_config: dict = Field(
        ...,
        examples=[{"keywords": ["Bảng lương", "Chiến lược kinh doanh"]}]
    )
    action: Literal["block", "alert", "log"]
    target_channel: Literal["usb", "browser", "email", "clipboard", "all"] = "all"
    target_departments: list[str] = ["all"]
    is_active: bool = True


class PolicyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    detection_type: Literal["keyword", "regex", "fingerprint"] | None = None
    detection_config: dict | None = None
    action: Literal["block", "alert", "log"] | None = None
    target_channel: Literal["usb", "browser", "email", "clipboard", "all"] | None = None
    target_departments: list[str] | None = None
    is_active: bool | None = None


class PolicyResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    detection_type: str
    detection_config: dict
    action: str
    target_channel: str
    target_departments: list[str]
    is_active: bool
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
