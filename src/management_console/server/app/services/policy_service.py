from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID
from app.models.policy import Policy
from app.models.agent import Agent
from app.models.agent_group import AgentGroup
from app.models.policy_assignment import policy_group_assignments, policy_agent_assignments

async def get_policies_by_group(db: AsyncSession, group_id: UUID):
    query = (
        select(Policy)
        .join(policy_group_assignments)
        .where(policy_group_assignments.c.group_id == group_id)
        .where(Policy.is_active == True)
    )
    result = await db.execute(query)
    return result.scalars().all()

async def get_combined_policies_for_agent(db: AsyncSession, agent_id: str):
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    
    if not agent:
        return []

    policy_map = {}

    if agent.group_id:
        group_policies = await get_policies_by_group(db, agent.group_id)
        for p in group_policies:
            policy_map[p.id] = p

    direct_query = (
        select(Policy)
        .join(policy_agent_assignments)
        .where(policy_agent_assignments.c.agent_id == agent_id)
        .where(Policy.is_active == True)
    )
    direct_result = await db.execute(direct_query)
    direct_policies = direct_result.scalars().all()
    
    for p in direct_policies:
        policy_map[p.id] = p

    return list(policy_map.values())