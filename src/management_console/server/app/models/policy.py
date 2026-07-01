import uuid
from uuid6 import uuid7
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, Text, Integer, Float
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
from sqlalchemy import func
from app.models.policy_assignment import policy_group_assignments, policy_agent_assignments

# Default action ladder (high -> low) for a new policy: format + context -> block,
# format only -> allow_log. Mirrors the analyzer's canonical bands (base 0.5 /
# context_boost 0.5 -> no-context 0.5, with-context 1.0).
_DEFAULT_ACTIONS = [
    {"min_score": 1.0, "action": "block"},
    {"min_score": 0.0, "action": "allow_log"},
]


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

    # End-user-facing block reason (shown on the browser popup / clipboard replacement /
    # Transfer Agent Note). Never the policy id. Empty -> agent uses a generic message.
    user_message: Mapped[str | None] = mapped_column(Text, server_default="")

    # Confidence scoring (the ONLY action mechanism, mirrors analyzer/policy.py):
    # a shape match scores `score_base`; a nearby context word adds `score_context_boost`;
    # the `actions` ladder [{min_score, action}, ...] maps the score -> action.
    score_base: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.5")
    score_context_boost: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.5")
    actions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=lambda: list(_DEFAULT_ACTIONS),
        server_default='[{"min_score": 1.0, "action": "block"}, {"min_score": 0.0, "action": "allow_log"}]',
    )

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
