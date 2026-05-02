from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.agent import Agent, AgentStatus
from app.models.user import User
from app.schemas.agent import AgentResponse, AgentCreate, AgentUpdate
from app.models.agent_group import AgentGroup
from app.api.deps import get_current_user, verify_agent_token
from app.services.policy_service import get_combined_policies_for_agent

router = APIRouter(prefix="/agents", tags=["Agents"])


@router.get("/", response_model=dict)
async def list_agents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: AgentStatus | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(Agent)
    if status:
        query = query.where(Agent.status == status)
    
    # pagination
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    agents = result.scalars().all()
    return { 
        "items": [AgentResponse.model_validate(a) for a in agents],
        "page": page,
        "page_size": page_size,
        }


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    
    agent.policies = await get_combined_policies_for_agent(db, str(agent_id))
    return AgentResponse.model_validate(agent)


@router.post("/register", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def register_agent(
    agent_data: AgentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    
    data = agent_data.model_dump(mode="json")
    agent = Agent(**data)
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return AgentResponse.model_validate(agent)


@router.patch("/{agent_id}/heartbeat", response_model=AgentResponse)
async def agent_heartbeat(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    agent_key: str = Depends(verify_agent_token)
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    agent.policies = await get_combined_policies_for_agent(db, str(agent_id))
    agent.status = AgentStatus.ACTIVE
    agent.updated_at = datetime.now(timezone.utc)   
    agent.last_seen = datetime.now(timezone.utc)
    


    await db.commit()
    return AgentResponse.model_validate(agent)


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: UUID,
    agent_data: AgentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    
    update_agent = agent_data.model_dump(exclude_unset=True)

    if "group_id" in update_agent and update_agent["group_id"] is not None:
        # Verify group exists
        result = await db.execute(select(AgentGroup).where(AgentGroup.id == update_agent["group_id"]))
        group = result.scalar_one_or_none()
        if not group:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent group not found")

    for key, value in update_agent.items():
        setattr(agent, key, value)
    
    await db.commit()
    return {"status": "ok"}


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    await db.delete(agent)
    await db.commit()
    return {"status": "ok", "message": f"Agent '{agent.hostname}' deleted successfully"}