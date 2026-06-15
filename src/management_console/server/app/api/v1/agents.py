import hashlib
import yaml
from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
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
import os
from pathlib import Path

router = APIRouter(prefix="/agents", tags=["Agents"])


def _compute_policies_hash(policies: list) -> str:
    """Compute a stable hash of the agent's policy set for change detection."""
    import json
    policy_dicts = []
    for p in policies:
        pd = {
            "id": str(p.id),
            "name": p.name,
            "rule_type": p.rule_type,
            "rule": p.rule,
            "action": p.action,
            "channel": p.channel,
            "is_active": p.is_active,
        }
        policy_dicts.append(pd)
    canonical = json.dumps(policy_dicts, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _compute_config_hash(agent_id: str) -> str:
    """Compute a hash of the agent's current config for change detection."""
    import json
    config_data = {
        "agent_id": agent_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    canonical = json.dumps(config_data, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@router.get("/", response_model=dict)
async def list_agents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None),
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

    total_agent_query = select(func.count(Agent.id))

    if search:
        query = query.where(Agent.hostname.ilike(f"%{search}%"))
        total_agent_query = total_agent_query.where(Agent.hostname.ilike(f"%{search}%"))
        
    total_agent_result = await db.execute(total_agent_query)
    total_agent = total_agent_result.scalar()

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
        "total": total_agent
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


@router.patch("/{agent_id}/heartbeat")
async def agent_heartbeat(
    agent_id: UUID,
    format: str = Query("json", enum=["json", "yaml"]),
    policies_hash: str | None = Query(None, description="Agent's local policies.yaml hash"),
    config_hash: str | None = Query(None, description="Agent's local config.yaml hash"),
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

    # Compute server-side hashes for change detection
    server_policies_hash = _compute_policies_hash(agent.policies)
    server_config_hash = _compute_config_hash(str(agent_id))

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
    
    return {
        **jsonable_encoder(agent_data),
        "policies_hash": server_policies_hash,
        "config_hash": server_config_hash,
    }


@router.post("/{agent_id}/logs")
async def upload_agent_logs(
    agent_id: UUID,
    events_file: UploadFile | None = File(None, description="events.jsonl file"),
    agent_log_file: UploadFile | None = File(None, description="dlp-agent.log file"),
    db: AsyncSession = Depends(get_db),
    agent_key: str = Depends(verify_agent_token)
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    log_dir = Path(os.getenv("AGENT_LOG_STORAGE", "logs/agents")) / str(agent_id)
    log_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []
    if events_file:
        events_path = log_dir / "events.jsonl"
        content = await events_file.read()
        events_path.write_bytes(content)
        uploaded.append("events.jsonl")

    if agent_log_file:
        agent_log_path = log_dir / "dlp-agent.log"
        content = await agent_log_file.read()
        agent_log_path.write_bytes(content)
        uploaded.append("dlp-agent.log")

    await add_audit_log(
        db=db,
        user_id="agent",
        username=f"agent_{agent.id}",
        action="upload_logs",
        target_type="agent",
        target_id=str(agent.id),
        description=f"Agent {agent.id} uploaded logs: {', '.join(uploaded)}"
    )
    await db.commit()

    return {"status": "ok", "uploaded": uploaded}


@router.get("/{agent_id}/status")
async def get_agent_status(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    now = datetime.now(timezone.utc)
    is_online = (now - agent.last_seen).total_seconds() < 300 if agent.last_seen else False

    return {
        "agent_id": str(agent.id),
        "hostname": agent.hostname,
        "status": agent.status.value,
        "is_online": is_online,
        "last_seen": agent.last_seen.isoformat() if agent.last_seen else None,
        "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
    }


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