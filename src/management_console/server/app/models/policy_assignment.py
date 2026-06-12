from sqlalchemy import Column, ForeignKey, Table
from app.database import Base

policy_group_assignments = Table(
    "policy_group_assignments",
    Base.metadata,
    Column("policy_id", ForeignKey("policies.id", ondelete="CASCADE"), primary_key=True),
    Column("agent_group_id", ForeignKey("agent_groups.id", ondelete="CASCADE"), primary_key=True),
)

policy_agent_assignments = Table(
    "policy_agent_assignments",
    Base.metadata,
    Column("policy_id", ForeignKey("policies.id", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True),
    Column("agent_id", ForeignKey("agents.id", onupdate="CASCADE", ondelete="CASCADE"), primary_key=True),
)