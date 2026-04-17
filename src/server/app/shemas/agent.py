# app/schemas/agent.py
from pydantic import BaseModel
from uuid import UUID
from datetime import datetime


class AgentRegister(BaseModel):
    """Agent tự đăng ký khi cài đặt lần đầu"""
    hostname: str
    ip_address: str | None = None
    os_info: str | None = None
    agent_version: str | None = None
    department: str | None = None


class AgentHeartbeat(BaseModel):
    """Agent gửi heartbeat định kỳ"""
    agent_id: UUID
    ip_address: str | None = None
    agent_version: str | None = None


class AgentResponse(BaseModel):
    id: UUID
    hostname: str
    ip_address: str | None
    os_info: str | None
    agent_version: str | None
    status: str
    department: str | None
    last_heartbeat: datetime | None
    registered_at: datetime

    model_config = {"from_attributes": True}
