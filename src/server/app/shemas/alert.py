# app/schemas/alert.py
from pydantic import BaseModel
from uuid import UUID
from datetime import datetime
from typing import Literal


class AlertCreate(BaseModel):
    """Schema mà Agent gửi lên khi phát hiện vi phạm"""
    agent_id: UUID
    policy_id: UUID
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    channel: str
    file_name: str | None = None
    file_path: str | None = None
    matched_content: str | None = None
    action_taken: Literal["block", "alert", "log"]


class AlertUpdateStatus(BaseModel):
    """Admin cập nhật trạng thái cảnh báo"""
    status: Literal["new", "reviewed", "resolved", "false_positive"]


class AlertResponse(BaseModel):
    id: UUID
    agent_id: UUID | None
    policy_id: UUID | None
    severity: str
    channel: str
    file_name: str | None
    file_path: str | None
    matched_content: str | None
    action_taken: str
    status: str
    triggered_at: datetime
    # Thông tin liên kết
    agent_hostname: str | None = None
    policy_name: str | None = None

    model_config = {"from_attributes": True}
