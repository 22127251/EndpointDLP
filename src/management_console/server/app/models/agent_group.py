import uuid
from uuid6 import uuid7
from datetime import datetime
from sqlalchemy import String, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class AgentGroup(Base):
    __tablename__ = "agent_groups"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    description: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    agents: Mapped[list["Agent"]] = relationship("Agent", back_populates="group", lazy="selectin")

    # Relationships
    policies = relationship("Policy",
        secondary="policy_group_assignments", back_populates="agent_groups", 
        lazy="selectin"
    )
