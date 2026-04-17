# app/api/v1/agents.py - Cập nhật endpoint lấy policies cho Agent

@router.get("/policies")
async def get_policies_for_agent(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Agent lấy danh sách policies áp dụng cho mình.
    Logic: Agent → AgentGroups → PolicyAssignments → PolicyGroups → Policies
    """
    from app.models.agent_group import AgentGroupMember
    from app.models.policy_group import PolicyAssignment, PolicyGroupMember
    from app.models.policy import Policy
    from app.schemas.policy import PolicyResponse

    # Bước 1: Tìm tất cả groups mà agent thuộc về
    agent_groups_result = await db.execute(
        select(AgentGroupMember.agent_group_id)
        .where(AgentGroupMember.agent_id == agent_id)
    )
    agent_group_ids = [row[0] for row in agent_groups_result.all()]

    if not agent_group_ids:
        return {"policies": [], "message": "Agent chưa thuộc group nào"}

    # Bước 2: Tìm tất cả PolicyGroup được gán cho các AgentGroup này
    assignments_result = await db.execute(
        select(PolicyAssignment.policy_group_id)
        .where(
            PolicyAssignment.agent_group_id.in_(agent_group_ids),
            PolicyAssignment.is_active == True
        )
    )
    policy_group_ids = [row[0] for row in assignments_result.all()]

    if not policy_group_ids:
        return {"policies": [], "message": "Chưa có policy group nào được gán"}

    # Bước 3: Lấy tất cả Policies từ các PolicyGroup
    policy_ids_result = await db.execute(
        select(PolicyGroupMember.policy_id, PolicyGroupMember.execution_order)
        .where(PolicyGroupMember.policy_group_id.in_(policy_group_ids))
        .order_by(PolicyGroupMember.execution_order)
    )
    policy_ids = list(set([row[0] for row in policy_ids_result.all()]))

    if not policy_ids:
        return {"policies": []}

    # Bước 4: Lấy chi tiết policies (chỉ lấy active)
    policies_result = await db.execute(
        select(Policy)
        .where(Policy.id.in_(policy_ids), Policy.is_active == True)
    )
    policies = policies_result.scalars().all()

    return {
        "policies": [PolicyResponse.model_validate(p) for p in policies],
        "total": len(policies)
    }
