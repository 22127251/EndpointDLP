# app/api/v1/dashboard.py
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.agent import Agent
from app.models.policy import Policy
from app.models.alert import Alert
from app.schemas.dashboard import DashboardStats, ChannelStats, SeverityStats
from app.api.deps import get_current_user
from app.models.user import User

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/stats", response_model=DashboardStats)
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lấy thống kê tổng quan cho Dashboard"""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Agents
    total_agents = (await db.execute(select(func.count(Agent.id)))).scalar()
    active_agents = (await db.execute(
        select(func.count(Agent.id)).where(Agent.status == "active")
    )).scalar()

    # Policies
    total_policies = (await db.execute(select(func.count(Policy.id)))).scalar()
    active_policies = (await db.execute(
        select(func.count(Policy.id)).where(Policy.is_active == True)
    )).scalar()

    # Alerts hôm nay
    total_alerts_today = (await db.execute(
        select(func.count(Alert.id)).where(Alert.triggered_at >= today_start)
    )).scalar()
    critical_alerts = (await db.execute(
        select(func.count(Alert.id)).where(
            Alert.triggered_at >= today_start,
            Alert.severity == "critical"
        )
    )).scalar()
    blocked_today = (await db.execute(
        select(func.count(Alert.id)).where(
            Alert.triggered_at >= today_start,
            Alert.action_taken == "block"
        )
    )).scalar()

    return DashboardStats(
        total_agents=total_agents,
        active_agents=active_agents,
        inactive_agents=total_agents - active_agents,
        total_policies=total_policies,
        active_policies=active_policies,
        total_alerts_today=total_alerts_today,
        critical_alerts_today=critical_alerts,
        total_blocked_today=blocked_today,
    )


@router.get("/alerts-by-channel", response_model=list[ChannelStats])
async def alerts_by_channel(
    days: int = 7,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Thống kê cảnh báo theo kênh rò rỉ (cho biểu đồ)"""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    query = (
        select(Alert.channel, func.count(Alert.id).label("count"))
        .where(Alert.triggered_at >= since)
        .group_by(Alert.channel)
        .order_by(func.count(Alert.id).desc())
    )
    result = await db.execute(query)
    return [ChannelStats(channel=row.channel, count=row.count) for row in result]


@router.get("/alerts-by-severity", response_model=list[SeverityStats])
async def alerts_by_severity(
    days: int = 7,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Thống kê cảnh báo theo mức độ nghiêm trọng"""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    query = (
        select(Alert.severity, func.count(Alert.id).label("count"))
        .where(Alert.triggered_at >= since)
        .group_by(Alert.severity)
    )
    result = await db.execute(query)
    return [SeverityStats(severity=row.severity, count=row.count) for row in result]
