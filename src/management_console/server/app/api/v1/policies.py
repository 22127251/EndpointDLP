from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models.policy import Policy
from app.models.user import User
from app.schemas.policy import PolicyCreate, PolicyUpdate, PolicyResponse, PolicyDetailResponse
from app.api.deps import get_current_user
from app.models.agent import Agent
from app.models.agent_group import AgentGroup
from app.services.audit_log_service import add_audit_log

router = APIRouter(prefix="/policies", tags=["Policies"])


@router.get("/", response_model=dict)
async def list_policies(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    
    
    query = (
        select(Policy)
        .options(
            selectinload(Policy.individual_agents),
            selectinload(Policy.agent_groups),
        )
    )

    total_query = select(func.count(Policy.id))
    if search:
        query = query.where(Policy.name.ilike(f"%{search}%"))
        total_query = total_query.where(Policy.name.ilike(f"%{search}%"))
        
    total_result = await db.execute(total_query)
    total = total_result.scalar()
    # pagination
    query = query.order_by(Policy.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    policies = result.scalars().all()

    return {
        "items": [PolicyResponse.model_validate(p) for p in policies],
        "page": page,
        "page_size": page_size,
        "total": total
    }


@router.post("/", response_model=PolicyResponse, status_code=status.HTTP_201_CREATED)
async def create_policy(
    policy_data: PolicyCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = policy_data.model_dump(mode="json")
    policy = Policy(**data)
    db.add(policy)

    # audit log
    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="create_policy",
        target_type="policy",
        target_id=str(policy.id),
        description=f"Created policy '{policy.name}' with ID {policy.id}"
    )

    await db.commit()
    await db.refresh(policy)
    return PolicyResponse.model_validate(policy)


@router.get("/{policy_id}", response_model=PolicyDetailResponse)
async def get_policy(
    policy_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Policy)
        .where(Policy.id == policy_id)
        .options(
            selectinload(Policy.individual_agents),
            selectinload(Policy.agent_groups),
        )
    )
    policy = result.scalar_one_or_none()
    if not policy:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")
    return PolicyDetailResponse.model_validate(policy)


@router.put("/{policy_id}", response_model=PolicyResponse)
async def update_policy(
    policy_id: UUID,
    policy_data: PolicyUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):

    policy = await db.get(Policy, policy_id)
    if not policy:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")
    
    update_data = policy_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(policy, key, value)


    # audit log
    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="update_policy",
        target_type="policy",
        target_id=str(policy.id),
        description=f"Updated policy '{policy.name}' with data {update_data}"
    )

    await db.commit()
    await db.refresh(policy)
    return PolicyResponse.model_validate(policy)


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    policy = await db.get(Policy, policy_id)
    if not policy:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")
    
    # audit log
    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="delete_policy",
        target_type="policy",
        target_id=str(policy.id),
        description=f"Deleted policy '{policy.name}' with ID {policy.id}"
    )

    await db.delete(policy)
    await db.commit()


@router.post("/{policy_id}/assign-agents", status_code=status.HTTP_200_OK)
async def assign_policy_to_agents(
    policy_id: UUID,
    agent_ids: list[UUID],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Policy)
        .where(Policy.id == policy_id)
        .options(selectinload(Policy.individual_agents))
    )
    policy = result.scalar_one_or_none()
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
    agents = result.scalars().all()
    
    policy.individual_agents = list(agents)

    # audit log
    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="assign_policy_to_agents",
        target_type="policy",
        target_id=str(policy.id),
        description=f"Assigned policy '{policy.name}' to {len(agents)} agents"
    )

    await db.commit()
    return {"message": f"Policy assigned to {len(agents)} agents"}


@router.post("/{policy_id}/assign-groups", status_code=status.HTTP_200_OK)
async def assign_policy_to_groups(
    policy_id: UUID,
    group_ids: list[UUID],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Policy)
        .where(Policy.id == policy_id)
        .options(selectinload(Policy.agent_groups))
    )
    policy = result.scalar_one_or_none()
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    result = await db.execute(select(AgentGroup).where(AgentGroup.id.in_(group_ids)))
    groups = result.scalars().all()
    
    policy.agent_groups = list(groups) 
    # audit log
    await add_audit_log(
        db=db,
        user_id=current_user.id,
        username=current_user.username,
        action="assign_policy_to_groups",
        target_type="policy",
        target_id=str(policy.id),
        description=f"Assigned policy '{policy.name}' to {len(groups)} groups"
    )
    await db.commit()
    return {"message": f"Policy assigned to {len(groups)} groups"}
