# app/api/v1/policy_groups.py
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Literal
from app.database import get_db
from app.models.policy_group import PolicyGroup, PolicyGroupMember, PolicyAssignment
from app.models.user import User
from app.api.deps import get_current_user

router = APIRouter(prefix="/policy-groups", tags=["Policy Groups"])


# --- Schemas ---
class PolicyGroupCreate(BaseModel):
    name: str
    description: str | None = None
    priority: Literal["low", "medium", "high", "critical"] = "medium"


class PolicyGroupResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    priority: str
    policy_count: int = 0
    assigned_agent_groups: list[str] = []

    model_config = {"from_attributes": True}


class AddPoliciesRequest(BaseModel):
    policy_ids: list[UUID]


class AssignRequest(BaseModel):
    """Gán PolicyGroup cho AgentGroup"""
    agent_group_id: UUID


# --- Endpoints ---
@router.get("/", response_model=list[PolicyGroupResponse])
async def list_policy_groups(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lấy danh sách Policy Groups"""
    result = await db.execute(select(PolicyGroup))
    groups = result.scalars().all()

    response = []
    for g in groups:
        pg = PolicyGroupResponse.model_validate(g)
        pg.policy_count = len(g.members)
        response.append(pg)
    return response


@router.post("/", response_model=PolicyGroupResponse, status_code=201)
async def create_policy_group(
    data: PolicyGroupCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Tạo Policy Group mới"""
    group = PolicyGroup(**data.model_dump(), created_by=current_user.id)
    db.add(group)
    await db.flush()
    await db.refresh(group)
    return PolicyGroupResponse.model_validate(group)


@router.post("/{group_id}/policies", status_code=201)
async def add_policies_to_group(
    group_id: UUID,
    data: AddPoliciesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Thêm nhiều Policy vào Group"""
    group = await db.get(PolicyGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Policy Group không tồn tại")

    added = 0
    for i, policy_id in enumerate(data.policy_ids):
        existing = await db.execute(
            select(PolicyGroupMember).where(
                PolicyGroupMember.policy_group_id == group_id,
                PolicyGroupMember.policy_id == policy_id
            )
        )
        if not existing.scalar_one_or_none():
            db.add(PolicyGroupMember(
                policy_group_id=group_id,
                policy_id=policy_id,
                execution_order=i
            ))
            added += 1

    await db.flush()
    return {"message": f"Đã thêm {added} policy vào group '{group.name}'"}


@router.post("/{group_id}/assign", status_code=201)
async def assign_to_agent_group(
    group_id: UUID,
    data: AssignRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Gán PolicyGroup cho AgentGroup - Đây là thao tác CHÍNH"""
    # Kiểm tra tồn tại
    policy_group = await db.get(PolicyGroup, group_id)
    if not policy_group:
        raise HTTPException(status_code=404, detail="Policy Group không tồn tại")

    from app.models.agent_group import AgentGroup
    agent_group = await db.get(AgentGroup, data.agent_group_id)
    if not agent_group:
        raise HTTPException(status_code=404, detail="Agent Group không tồn tại")

    # Kiểm tra đã gán chưa
    existing = await db.execute(
        select(PolicyAssignment).where(
            PolicyAssignment.policy_group_id == group_id,
            PolicyAssignment.agent_group_id == data.agent_group_id
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Đã gán rồi")

    assignment = PolicyAssignment(
        policy_group_id=group_id,
        agent_group_id=data.agent_group_id,
        assigned_by=current_user.id
    )
    db.add(assignment)
    await db.flush()

    return {
        "message": f"Đã gán '{policy_group.name}' cho '{agent_group.name}'",
        "assignment_id": str(assignment.id)
    }


@router.delete("/{group_id}/assign/{agent_group_id}", status_code=204)
async def unassign_from_agent_group(
    group_id: UUID,
    agent_group_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hủy gán PolicyGroup khỏi AgentGroup"""
    result = await db.execute(
        select(PolicyAssignment).where(
            PolicyAssignment.policy_group_id == group_id,
            PolicyAssignment.agent_group_id == agent_group_id
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment không tồn tại")
    await db.delete(assignment)
