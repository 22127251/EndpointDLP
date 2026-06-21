from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from pydantic import BaseModel
from app.database import get_db
from app.models.agent_group import AgentGroup
from app.models.user import User
from app.api.deps import get_current_user
from app.schemas.agent_group import AgentGroupCreate, AgentGroupResponse, AddMembersRequest
from sqlalchemy.orm import selectinload
from app.models.agent import Agent
from app.services.audit_log_service import add_audit_log

router = APIRouter(prefix="/agent-groups", tags=["Agent Groups"])


@router.get("/", response_model=dict)
async def list_agent_groups(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search : str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = (
        select(AgentGroup)
        .options(selectinload(AgentGroup.agents), selectinload(AgentGroup.policies))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    total_query = select(func.count(AgentGroup.id))
    if search:
        query = query.where(AgentGroup.name.ilike(f"%{search}%"))

        total_query = total_query.where(AgentGroup.name.ilike(f"%{search}%"))

    total_result = await db.execute(total_query)
    total = total_result.scalar()
    result = await db.execute(query)
    groups = result.scalars().all()

    items = []
    for group in groups:
        item = AgentGroupResponse.model_validate(group)
        item.member_count = len(group.agents)
        items.append(item)

    return {
        "items": items,
        "page": page, 
        "page_size": page_size,
        "total": total
    }


@router.put("/{group_id}", response_model=AgentGroupResponse)
async def update_agent_group(
    group_id: UUID,
    data: AgentGroupCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    group = await db.get(AgentGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group does not exist")

    group.name = data.name
    group.description = data.description

    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="update_agent_group",
        target_type="agent_group",
        target_id=str(group_id),
        description=f"Updated agent group '{group.name}'"
    )

    await db.commit()
    await db.refresh(group)
    result = await db.execute(
        select(AgentGroup)
        .options(selectinload(AgentGroup.agents))
        .where(AgentGroup.id == group_id)
    )
    group_with_agents = result.scalar_one()
    return AgentGroupResponse(
        id=group.id, name=group.name,
        description=group.description,
        member_count=len(group_with_agents.agents),
        agents=[AgentResponse.model_validate(a) for a in group_with_agents.agents],
    )


@router.post("/", response_model=AgentGroupResponse, status_code=201)
async def create_agent_group(
    data: AgentGroupCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    group = AgentGroup(**data.model_dump())
    db.add(group)

    # audit log
    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="create_agent_group",
        target_type="agent_group",
        target_id=None,
        description=f"Created agent group '{group.name}' with ID {group.id}"
    )

    await db.commit()
    await db.refresh(group)
    return AgentGroupResponse(
        id=group.id, name=group.name,
        description=group.description, member_count=0, agents=[]
    )


@router.post("/{group_id}/members", status_code=201)
async def add_agents_to_group(
    group_id: UUID,
    data: AddMembersRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    group = await db.get(AgentGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group does not exist")
    
    result = await db.execute(
        select(Agent).where(Agent.id.in_(data.agent_ids))
    )
    agents = result.scalars().all()

    already_assigned = [a.hostname for a in agents if a.group_id is not None]
    
    if already_assigned:
        raise HTTPException(
            status_code=400, 
            detail=f"Agents already assigned to another group: {', '.join(already_assigned)}"
        )

    if not agents:
        raise HTTPException(status_code=404, detail="No agents found with provided IDs")

    for agent in agents:
        agent.group_id = group_id


    # audit log
    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="add_agents_to_group",
        target_type="agent_group",
        target_id=str(group_id),
        description=f"Added agents to group '{group.name}' with ID {group.id}"
    )

    await db.commit()

    return {"message": f"Added {len(agents)} new agents to group '{group.name}'"}


@router.delete("/{group_id}/members/{agent_id}", status_code=204)
async def remove_agent_from_group(
    group_id: UUID,
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await db.get(Agent, agent_id)
    if not agent or agent.group_id != group_id:
        raise HTTPException(status_code=404, detail="Agent not found in the specified group")
    
    agent.group_id = None

    # audit log
    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="remove_agent_from_group",
        target_type="agent_group",
        target_id=str(group_id),
        description=f"Removed agent '{agent.hostname}' with ID {agent.id} from group ID {group_id}"
    )

    await db.commit()

    return {"message": f"Agent '{agent.hostname}' removed from group successfully"}


@router.delete("/{group_id}", status_code=204)
async def delete_agent_group(
    group_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    group = await db.get(AgentGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group does not exist")
    
    # audit log
    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="delete_agent_group",
        target_type="agent_group",
        target_id=str(group_id),
        description=f"Deleted agent group '{group.name}' with ID {group.id}"
    )

    await db.delete(group)
    await db.commit()
    return {"message": f"Group '{group.name}' deleted successfully"}
