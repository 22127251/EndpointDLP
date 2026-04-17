# app/models/agent_group.py
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class AgentGroup(Base):
    __tablename__ = "agent_groups"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_groups.id", ondelete="SET NULL")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    parent = relationship("AgentGroup", remote_side="AgentGroup.id", lazy="selectin")
    children = relationship("AgentGroup", back_populates="parent", lazy="selectin")
    members = relationship("AgentGroupMember", back_populates="group", lazy="selectin")


class AgentGroupMember(Base):
    __tablename__ = "agent_group_members"
    __table_args__ = (
        UniqueConstraint("agent_group_id", "agent_id", name="uq_agent_group_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agent_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_groups.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    group = relationship("AgentGroup", back_populates="members")
    agent = relationship("Agent", lazy="selectin")
