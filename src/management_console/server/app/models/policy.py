import uuid
from uuid6 import uuid7
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, Text, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
from sqlalchemy import func
from app.models.policy_assignment import policy_group_assignments, policy_agent_assignments


class Policy(Base):
    """DLP Policy — format matches local analyzer/policies.yaml exactly."""
    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Rule type: "regex" | "keyword" | "denylist"
    type: Mapped[str] = mapped_column(String(50), nullable=False, default="regex")

    # Patterns for regex type, keywords for denylist/keyword type
    patterns: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    keywords: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Channels: ["browser", "clipboard", "peripheral_storage"]
    channels: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Action: "block" | "allow" | "allow_log"
    action: Mapped[str] = mapped_column(String(50), nullable=False, default="block")

    # Context matching (optional)
    context_words: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    context_range: Mapped[int] = mapped_column(Integer, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )

    agent_groups = relationship("AgentGroup", secondary=policy_group_assignments, back_populates="policies")
    individual_agents = relationship("Agent", secondary=policy_agent_assignments, back_populates="policies")
