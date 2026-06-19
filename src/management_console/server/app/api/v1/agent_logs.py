from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone
from app.database import get_db
from app.models.agent import Agent
from app.models.agent_log import AgentLog
from app.schemas.agent_log import AgentLogPush

router = APIRouter(prefix="/agents", tags=["Agent Logs"])


@router.post("/{agent_id}/logs", status_code=status.HTTP_201_CREATED)
async def push_agent_logs(
    agent_id: UUID,
    body: AgentLogPush,
    db: AsyncSession = Depends(get_db),
):
    # Verify agent exists
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found"
        )

    # Update last_seen
    agent.last_seen = datetime.now(timezone.utc)

    # Store events tail
    if body.events_tail:
        log_entry = AgentLog(
            agent_id=agent_id,
            log_type="events",
            content=body.events_tail,
            byte_offset=0,
        )
        db.add(log_entry)

    # Store agent log tail
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
