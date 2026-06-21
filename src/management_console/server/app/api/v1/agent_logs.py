from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import datetime, timezone
from app.database import get_db
from app.models.agent import Agent
from app.models.agent_log import AgentLog
from app.models.user import User
from app.schemas.agent_log import AgentLogPush, AgentLogResponse
from app.api.deps import get_current_user

router = APIRouter(prefix="/agents", tags=["Agent Logs"])


@router.post("/{agent_id}/logs", status_code=status.HTTP_201_CREATED)
async def push_agent_logs(
    agent_id: UUID,
    body: AgentLogPush,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found"
        )

    agent.last_seen = datetime.now(timezone.utc)

    if body.events_tail:
        log_entry = AgentLog(
            agent_id=agent_id,
            log_type="events",
            content=body.events_tail,
            byte_offset=0,
        )
        db.add(log_entry)

    if body.agent_log_tail:
        log_entry = AgentLog(
            agent_id=agent_id,
            log_type="agent_log",
            content=body.agent_log_tail,
            byte_offset=0,
        )
        db.add(log_entry)

    await db.commit()
    return {"ok": True}


@router.get("/{agent_id}/logs", response_model=dict)
async def list_agent_logs(
    agent_id: UUID,
    log_type: str | None = Query(None, enum=["events", "agent_log"]),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    query = select(AgentLog).where(AgentLog.agent_id == agent_id)
    total_query = select(func.count(AgentLog.id)).where(AgentLog.agent_id == agent_id)

    if log_type:
        query = query.where(AgentLog.log_type == log_type)
        total_query = total_query.where(AgentLog.log_type == log_type)

    total_result = await db.execute(total_query)
    total = total_result.scalar()

    query = query.order_by(AgentLog.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    logs = result.scalars().all()

    return {
        "items": [AgentLogResponse.model_validate(l) for l in logs],
        "page": page,
        "page_size": page_size,
        "total": total,
    }
