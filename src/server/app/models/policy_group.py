# app/models/policy_group.py
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Text, Integer, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class PolicyGroup(Base):
    __tablename__ = "policy_groups"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    members = relationship("PolicyGroupMember", back_populates="group", lazy="selectin")


class PolicyGroupMember(Base):
    __tablename__ = "policy_group_members"
    __table_args__ = (
        UniqueConstraint("policy_group_id", "policy_id", name="uq_policy_group_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    policy_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policy_groups.id", ondelete="CASCADE"), nullable=False
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policies.id", ondelete="CASCADE"), nullable=False
    )
    execution_order: Mapped[int] = mapped_column(Integer, default=0)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    group = relationship("PolicyGroup", back_populates="members")
    policy = relationship("Policy", lazy="selectin")


class PolicyAssignment(Base):
    __tablename__ = "policy_assignments"
    __table_args__ = (
        UniqueConstraint("policy_group_id", "agent_group_id", name="uq_policy_assignment"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    policy_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policy_groups.id", ondelete="CASCADE"), nullable=False
    )
    agent_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_groups.id", ondelete="CASCADE"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    policy_group = relationship("PolicyGroup", lazy="selectin")
    agent_group = relationship("AgentGroup", lazy="selectin")
