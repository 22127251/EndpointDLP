from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from app.database import get_db
from app.models.violation_log import ViolationLog
from app.models.agent import Agent
from app.models.policy import Policy
from app.schemas.violation_log import ViolationLogCreate
from app.api.deps import get_current_user
from app.models.user import User



router = APIRouter(prefix="/violation-logs", tags=["Logs"])


@router.get("/")
async def list_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    search: str | None = Query(None), 
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(ViolationLog).order_by(desc(ViolationLog.created_at))

    total_query = select(func.count(ViolationLog.id))

    if search:
        query = query.where(ViolationLog.action.ilike(f"%{search}%"))
        total_query = total_query.where(ViolationLog.action.ilike(f"%{search}%"))
    
    # Pagination
    query = query.offset((page - 1) * page_size).limit(page_size)

    total_result = await db.execute(total_query)
    total = total_result.scalar()
    
    result = await db.execute(query)
    logs = result.scalars().all()
    
    return {
        "items": logs,
        "page": page,
        "page_size": page_size,
        "total": total
    }


@router.post("/", status_code=201)
async def create_violation_log(
    data: ViolationLogCreate,
    db: AsyncSession = Depends(get_db),
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

