import yaml
from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models.agent import Agent, AgentStatus
from app.models.user import User
from app.schemas.agent import AgentResponse, AgentCreate, AgentUpdate
from app.models.agent_group import AgentGroup
from app.api.deps import get_current_user, verify_agent_token
from app.services.policy_service import get_combined_policies_for_agent
from fastapi.encoders import jsonable_encoder
from app.services.audit_log_service import add_audit_log

router = APIRouter(prefix="/agents", tags=["Agents"])


@router.get("/", response_model=dict)
async def list_agents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: AgentStatus | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = (
        select(Agent)
        .options(
            selectinload(Agent.group),
            selectinload(Agent.group).selectinload(AgentGroup.policies)
        )
    )

    if status:
        query = query.where(Agent.status == status)
    
    # pagination
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    agents = result.scalars().all()

    items = []
    for agent in agents:
        policy_map = {}

        if agent.group_id and agent.group.policies:
            for p in agent.group.policies:
                if p.is_active:
                    policy_map[p.id] = p

        if agent.policies:
            for p in agent.policies:
                if p.is_active:
                    policy_map[p.id] = p

        policies = await get_combined_policies_for_agent(db, str(agent.id))
        agent.policies = policies
        items.append(AgentResponse.model_validate(agent))


    return { 
        "items": [AgentResponse.model_validate(a) for a in agents],
        "page": page,
        "page_size": page_size,
        "total": len(items)
    }


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Agent)
        .where(Agent.id == agent_id)
        .options(selectinload(Agent.group))
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    
    policies = await get_combined_policies_for_agent(db, str(agent_id))

    agent.policies = policies
    

    return AgentResponse.model_validate(agent)

@router.get("/{agent_id}/config")
async def get_agent_config(
    agent_id: UUID,
    format: str = Query("json", enum=["json", "yaml"]),
    db: AsyncSession = Depends(get_db),
    agent_key: str = Depends(verify_agent_token)
):
    result = await db.execute(
        select(Agent)
        .where(Agent.id == agent_id)
        .options(selectinload(Agent.group))
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    
    policies = await get_combined_policies_for_agent(db, str(agent_id))

    agent.policies = policies

    agent_data = AgentResponse.model_validate(agent)
    if format == "yaml":
        clean_data = jsonable_encoder(agent_data)
        yaml_content = yaml.dump(
            clean_data, 
            sort_keys=False, 
            allow_unicode=True, 
            default_flow_style=False
        )

        # audit log
        await add_audit_log(
            db=db,
            user_id="agent",
            username=f"agent_{agent.id}",
            action="download_config",
            target_type="agent",
            target_id=str(agent.id),
            description=f"Agent {agent.id} downloaded its configuration in YAML format"
        )
        await db.commit()
        
        return Response(
            content=yaml_content,
            media_type="application/x-yaml",
            headers={
                "Content-Disposition": f"attachment; filename=agent_{agent.id}_config.yaml"
            }
        )
    
    # audit log
    await add_audit_log(
        db=db,
        user_id="agent",
        username=f"agent_{agent.id}",
        action="download_config",
        target_type="agent",
        target_id=str(agent.id),
        description=f"Agent {agent.id} downloaded its configuration in JSON format"
    )
    await db.commit()

    return agent_data

@router.post("/register", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def register_agent(
    agent_data: AgentCreate,
    db: AsyncSession = Depends(get_db),
    agent_key: str = Depends(verify_agent_token)
):
    data = agent_data.model_dump(mode="json")
    
    agent = Agent(**data)
    db.add(agent)
    
    # audit log
    await add_audit_log(
        db=db,
        user_id="agent",
        username=f"agent_{agent.hostname}",
        action="register",
        target_type="agent",
        target_id=str(agent.id),
        description=f"Registered new agent with hostname {agent.hostname}"
    )


    await db.commit()
    await db.refresh(agent)
    return AgentResponse.model_validate(agent)


@router.patch("/{agent_id}/heartbeat", response_model=AgentResponse)
async def agent_heartbeat(
    agent_id: UUID,
    format: str = Query("json", enum=["json", "yaml"]),
    db: AsyncSession = Depends(get_db),
    agent_key: str = Depends(verify_agent_token)
):
    result = await db.execute(
        select(Agent)
        .where(Agent.id == agent_id)
        .options(selectinload(Agent.group))
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    
    agent.policies = await get_combined_policies_for_agent(db, str(agent_id))
    agent.status = AgentStatus.ACTIVE
    agent.updated_at = datetime.now(timezone.utc)   
    agent.last_seen = datetime.now(timezone.utc)

    await db.commit()

    agent_data = AgentResponse.model_validate(agent)
    if format == "yaml":
        clean_data = jsonable_encoder(agent_data)
        yaml_content = yaml.dump(
            clean_data, 
            sort_keys=False, 
            allow_unicode=True, 
            default_flow_style=False
        )
        
        return Response(
            content=yaml_content,
            media_type="application/x-yaml",
            headers={
                "Content-Disposition": f"attachment; filename=agent_{agent.id}_config.yaml"
            }
        )
    
    return agent_data


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
        result = await db.execute(select(AgentGroup).where(AgentGroup.id == update_agent["group_id"]))
        group = result.scalar_one_or_none()
        if not group:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent group not found")

    for key, value in update_agent.items():
        setattr(agent, key, value)
    
    # audit log
    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="update_agent",
        target_type="agent",
        target_id=str(agent.id),
        description=f"Updated agent {agent.id} with data {update_agent}"
    )

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

    # audit log
    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="delete_agent",
        target_type="agent",
        target_id=str(agent.id),
        description=f"Deleted agent {agent.hostname} with ID {agent.id}"
    )
    await db.delete(agent)
    await db.commit()
    return {"status": "ok", "message": f"Agent '{agent.hostname}' deleted successfully"}