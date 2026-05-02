import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
from sqlalchemy import func, Enum
from enum import StrEnum



class AgentStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    OFFLINE = "offline"

class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    description: Mapped[str | None] = mapped_column(Text)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[AgentStatus] = mapped_column(
        String(20), nullable=False,
        default=AgentStatus.INACTIVE
    )
    
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    group = relationship("AgentGroup", back_populates="agents", lazy="selectin")
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_groups.id", ondelete="SET NULL"))
    policies = relationship("Policy", secondary="policy_agent_assignments", back_populates="individual_agents", lazy="selectin")
