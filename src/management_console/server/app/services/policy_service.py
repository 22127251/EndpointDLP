from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from uuid import UUID
from app.models.policy import Policy
from app.models.agent import Agent
from app.models.agent_group import AgentGroup


async def get_combined_policies_for_agent(db: AsyncSession, agent_id: str):
    agent_result = await db.execute(
        select(Agent)
        .where(Agent.id == agent_id)
        .options(
            selectinload(Agent.group),
            selectinload(Agent.group).selectinload(AgentGroup.policies),
        )
    )
    agent = agent_result.scalar_one_or_none()
    
    if not agent:
        return []

    policy_map = {}

    if agent.group_id:
        for p in agent.group.policies:
            if p.is_active:
                policy_map[p.id] = p

    if agent.policies:
        for p in agent.policies:
            if p.is_active:
                policy_map[p.id] = p

    return list(policy_map.values())