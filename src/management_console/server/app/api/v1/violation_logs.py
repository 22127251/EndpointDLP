from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, or_
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models.violation_log import ViolationLog
from app.models.violation_policy_match import ViolationPolicyMatch
from app.models.agent import Agent
from app.models.policy import Policy
from app.schemas.violation_log import ViolationLogCreate, ViolationLogResponse
from app.api.deps import get_current_user
from app.models.user import User


router = APIRouter(prefix="/violation-logs", tags=["Logs"])


@router.get("/")
async def list_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    search: str | None = Query(None),
    reason: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = (
        select(ViolationLog)
        .options(
            selectinload(ViolationLog.agent),
            selectinload(ViolationLog.matches).selectinload(ViolationPolicyMatch.policy),
        )
        .order_by(desc(ViolationLog.created_at))
    )
    total_query = select(func.count(ViolationLog.id))

    # Audit filter: exact reason category (policy_violation / timeout / oversize / ...).
    if reason:
        query = query.where(ViolationLog.reason == reason)
        total_query = total_query.where(ViolationLog.reason == reason)

    if search:
        cond = or_(
            ViolationLog.decision.ilike(f"%{search}%"),
            ViolationLog.reason.ilike(f"%{search}%"),
            ViolationLog.channel.ilike(f"%{search}%"),
        )
        query = query.where(cond)
        total_query = total_query.where(cond)

    query = query.offset((page - 1) * page_size).limit(page_size)

    total = (await db.execute(total_query)).scalar()
    logs = (await db.execute(query)).scalars().all()

    return {
        "items": [ViolationLogResponse.model_validate(l) for l in logs],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.post("/", status_code=201)
async def create_violation_log(
    data: ViolationLogCreate,
    db: AsyncSession = Depends(get_db),
):
    """Record one notable agent decision: a parent event row + one child per triggered
    policy. A match whose policy no longer exists is kept with ``policy_id=NULL``."""
    agent = await db.get(Agent, data.agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    match_rows = []
    for m in data.matches:
        policy_id = m.policy_id
        if policy_id is not None and await db.get(Policy, policy_id) is None:
            policy_id = None  # deleted policy -> keep the match, drop the FK
        match_rows.append(ViolationPolicyMatch(
            policy_id=policy_id,
            action=m.action,
            count=m.count,
            with_context=m.with_context,
            context_words_triggered=m.context_words_triggered,
        ))

    event = ViolationLog(
        agent_id=data.agent_id,
        channel=data.channel,
        decision=data.decision,
        reason=data.reason,
        details=data.details or {},
        matches=match_rows,
    )
    db.add(event)
    await db.commit()
    return {"status": "received", "log_id": str(event.id), "matches": len(match_rows)}
