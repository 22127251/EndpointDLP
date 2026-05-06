import uuid
from uuid6 import uuid7
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, Text, Enum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
from sqlalchemy import func
from app.models.policy_assignment import policy_group_assignments, policy_agent_assignments


    
class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    description: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(50), nullable=False)
    rule: Mapped[dict] = mapped_column(JSONB, nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    channel: Mapped[str] = mapped_column(String(50), default="all" )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    
    agent_groups = relationship("AgentGroup", secondary=policy_group_assignments, back_populates="policies")
    individual_agents = relationship("Agent", secondary=policy_agent_assignments, back_populates="policies")