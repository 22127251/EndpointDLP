from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.database import get_db
from app.models.log import ViolationLog
from app.models.agent import Agent
from app.models.policy import Policy
from app.schemas.log import ViolationLogCreate, ViolationLogResponse
from app.api.deps import verify_agent_token, get_current_user
from app.models.user import User


router = APIRouter(prefix="/logs", tags=["Logs"])

@router.post("/", status_code=201)
async def create_violation_log(
    data: ViolationLogCreate,
    db: AsyncSession = Depends(get_db),
    agent_key: str = Depends(verify_agent_token)
):
    agent = await db.get(Agent, data.agent_id)
    policy = await db.get(Policy, data.policy_id)
    
    if not agent or not policy:
        return {"status": "error", "message": "Agent or Policy not found"}

    new_log = ViolationLog(**data.model_dump(mode="json"))
    
    db.add(new_log)
    await db.commit()
    await db.refresh(new_log)
    return {"status": "received", "log_id": new_log.id}

@router.get("/")
async def list_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(ViolationLog).order_by(desc(ViolationLog.created_at))
    
    # Pagination
    query = query.offset((page - 1) * page_size).limit(page_size)
    
    result = await db.execute(query)
    logs = result.scalars().all()
    
    return {
        "items": logs,
        "page": page,
        "page_size": page_size
    }