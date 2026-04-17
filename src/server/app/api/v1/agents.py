# app/api/v1/agents.py
from uuid import UUID
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.schemas.agent import AgentRegister, AgentHeartbeat, AgentResponse
from app.api.deps import get_current_user

router = APIRouter(prefix="/agents", tags=["Agents"])


@router.get("/", response_model=list[AgentResponse])
async def list_agents(
    status: str | None = None,
    department: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Xem danh sách các máy đã cài Agent"""
    query = select(Agent)
    if status:
        query = query.where(Agent.status == status)
    if department:
        query = query.where(Agent.department == department)
    query = query.order_by(Agent.last_heartbeat.desc().nullslast())

    result = await db.execute(query)
    agents = result.scalars().all()
    return [AgentResponse.model_validate(a) for a in agents]


@router.post("/register", response_model=AgentResponse, status_code=201)
async def register_agent(
    agent_data: AgentRegister,
    db: AsyncSession = Depends(get_db),
):
    """Agent tự đăng ký khi cài đặt lần đầu"""
    agent = Agent(
        **agent_data.model_dump(),
        status="active",
        last_heartbeat=datetime.now(timezone.utc)
    )
    db.add(agent)
    await db.flush()
    await db.refresh(agent)
    return AgentResponse.model_validate(agent)


@router.post("/heartbeat")
async def agent_heartbeat(
    heartbeat: AgentHeartbeat,
    db: AsyncSession = Depends(get_db),
):
    """Agent gửi heartbeat định kỳ (mỗi 1-5 phút)"""
    result = await db.execute(select(Agent).where(Agent.id == heartbeat.agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent chưa đăng ký")

    agent.status = "active"
    agent.last_heartbeat = datetime.now(timezone.utc)
    if heartbeat.ip_address:
        agent.ip_address = heartbeat.ip_address
    if heartbeat.agent_version:
        agent.agent_version = heartbeat.agent_version

    await db.flush()
    return {"status": "ok"}


@router.get("/policies")
async def get_policies_for_agent(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Agent lấy danh sách chính sách đang active để áp dụng"""
    from app.models.policy import Policy
    from app.schemas.policy import PolicyResponse

    # Kiểm tra agent tồn tại
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent không tồn tại")

    # Lấy chính sách active, lọc theo department
    query = select(Policy).where(Policy.is_active == True)
    result = await db.execute(query)
    all_policies = result.scalars().all()

    # Lọc theo department của agent
    applicable = []
    for p in all_policies:
        depts = p.target_departments or ["all"]
        if "all" in depts or (agent.department and agent.department in depts):
            applicable.append(PolicyResponse.model_validate(p))

    return {"policies": applicable}
