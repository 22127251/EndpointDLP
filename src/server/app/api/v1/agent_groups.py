# app/api/v1/agent_groups.py
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from app.database import get_db
from app.models.agent_group import AgentGroup, AgentGroupMember
from app.models.user import User
from app.api.deps import get_current_user

router = APIRouter(prefix="/agent-groups", tags=["Agent Groups"])


# --- Schemas ---
class AgentGroupCreate(BaseModel):
    name: str
    description: str | None = None
    parent_id: UUID | None = None


class AgentGroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    parent_id: UUID | None = None


class AgentGroupResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    parent_id: UUID | None
    member_count: int = 0
    children: list["AgentGroupResponse"] = []

    model_config = {"from_attributes": True}


class AddMembersRequest(BaseModel):
    agent_ids: list[UUID]


# --- Endpoints ---
@router.get("/", response_model=list[AgentGroupResponse])
async def list_agent_groups(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lấy danh sách tất cả Agent Groups (dạng cây)"""
    result = await db.execute(
        select(AgentGroup).where(AgentGroup.parent_id.is_(None))
    )
    root_groups = result.scalars().all()

    groups = []
    for g in root_groups:
        group_data = AgentGroupResponse.model_validate(g)
        group_data.member_count = len(g.members)
        # Build children tree
        group_data.children = [
            AgentGroupResponse(
                id=c.id, name=c.name, description=c.description,
                parent_id=c.parent_id, member_count=len(c.members)
            ) for c in g.children
        ]
        groups.append(group_data)
    return groups


@router.post("/", response_model=AgentGroupResponse, status_code=201)
async def create_agent_group(
    data: AgentGroupCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Tạo Agent Group mới"""
    # Kiểm tra parent tồn tại
    if data.parent_id:
        parent = await db.get(AgentGroup, data.parent_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Parent group không tồn tại")

    group = AgentGroup(**data.model_dump(), created_by=current_user.id)
    db.add(group)
    await db.flush()
    await db.refresh(group)
    return AgentGroupResponse(
        id=group.id, name=group.name,
        description=group.description, parent_id=group.parent_id
    )


@router.post("/{group_id}/members", status_code=201)
async def add_agents_to_group(
    group_id: UUID,
    data: AddMembersRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Thêm nhiều Agent vào Group"""
    group = await db.get(AgentGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group không tồn tại")

    added = 0
    for agent_id in data.agent_ids:
        # Kiểm tra đã tồn tại chưa
        existing = await db.execute(
            select(AgentGroupMember).where(
                AgentGroupMember.agent_group_id == group_id,
                AgentGroupMember.agent_id == agent_id
            )
        )
        if not existing.scalar_one_or_none():
            db.add(AgentGroupMember(agent_group_id=group_id, agent_id=agent_id))
            added += 1

    await db.flush()
    return {"message": f"Đã thêm {added} agent vào group '{group.name}'"}


@router.delete("/{group_id}/members/{agent_id}", status_code=204)
async def remove_agent_from_group(
    group_id: UUID,
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Xóa Agent khỏi Group"""
    result = await db.execute(
        select(AgentGroupMember).where(
            AgentGroupMember.agent_group_id == group_id,
            AgentGroupMember.agent_id == agent_id
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Agent không thuộc group này")
    await db.delete(member)


@router.delete("/{group_id}", status_code=204)
async def delete_agent_group(
    group_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Xóa Agent Group (CASCADE xóa members và assignments)"""
    group = await db.get(AgentGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group không tồn tại")
    await db.delete(group)
