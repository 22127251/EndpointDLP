# app/schemas/dashboard.py
from pydantic import BaseModel


class DashboardStats(BaseModel):
    total_agents: int
    active_agents: int
    inactive_agents: int
    total_policies: int
    active_policies: int
    total_alerts_today: int
    critical_alerts_today: int
    total_blocked_today: int


class ChannelStats(BaseModel):
    channel: str
    count: int


class SeverityStats(BaseModel):
    severity: str
    count: int
