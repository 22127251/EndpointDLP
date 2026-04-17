# app/api/v1/alerts.py
from uuid import UUID
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.alert import Alert
from app.models.user import User
from app.schemas.alert import AlertCreate, AlertUpdateStatus, AlertResponse
from app.api.deps import get_current_user

router = APIRouter(prefix="/alerts", tags=["Alerts"])


@router.get("/", response_model=dict)
async def list_alerts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    severity: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    channel: str | None = None,
    agent_id: UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lấy danh sách cảnh báo với bộ lọc nâng cao"""
    query = select(Alert)

    if severity:
        query = query.where(Alert.severity == severity)
    if status_filter:
        query = query.where(Alert.status == status_filter)
    if channel:
        query = query.where(Alert.channel == channel)
    if agent_id:
        query = query.where(Alert.agent_id == agent_id)
    if from_date:
        query = query.where(Alert.triggered_at >= from_date)
    if to_date:
        query = query.where(Alert.triggered_at <= to_date)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar()

    query = query.order_by(Alert.triggered_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    alerts = result.scalars().all()

    items = []
    for alert in alerts:
        data = AlertResponse.model_validate(alert)
        # Gắn thêm thông tin từ relationship
        data.agent_hostname = alert.agent.hostname if alert.agent else None
        data.policy_name = alert.policy.name if alert.policy else None
        items.append(data)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/", response_model=AlertResponse, status_code=201)
async def create_alert(
    alert_data: AlertCreate,
    db: AsyncSession = Depends(get_db),
    # Lưu ý: Endpoint này sẽ dùng API Key auth cho Agent, không phải JWT
):
    """Agent gửi cảnh báo về Management Console"""
    alert = Alert(**alert_data.model_dump())
    db.add(alert)
    await db.flush()
    await db.refresh(alert)
    return AlertResponse.model_validate(alert)


@router.patch("/{alert_id}/status", response_model=AlertResponse)
async def update_alert_status(
    alert_id: UUID,
    status_update: AlertUpdateStatus,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Admin đánh dấu trạng thái cảnh báo"""
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="Không tìm thấy cảnh báo")

    alert.status = status_update.status
    await db.flush()
    await db.refresh(alert)
    return AlertResponse.model_validate(alert)
