import uuid
from sqlalchemy import String, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB
from app.database import Base
from uuid6 import uuid7


class ViolationPolicyMatch(Base):
    """One triggered policy within a violation event. Mirrors a single entry of the
    agent's events.jsonl ``violations`` list."""
    __tablename__ = "violation_policy_matches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    violation_log_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("violation_logs.id", ondelete="CASCADE")
    )
    # SET NULL (nullable) so a deleted policy keeps the audit row.
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policies.id", ondelete="SET NULL"), nullable=True
    )
    # The action this policy resolved to: block | allow_log | allow.
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    # count = shape matches for this policy; with_context = how many had a boost.
    count: Mapped[int] = mapped_column(Integer, default=0)
    with_context: Mapped[int] = mapped_column(Integer, default=0)
    # The distinct context words that triggered a boost (NOT the policy's full list).
    context_words_triggered: Mapped[list] = mapped_column(JSONB, default=list)

    violation = relationship("ViolationLog", back_populates="matches")
    policy = relationship("Policy")

    @property
    def policy_name(self) -> str | None:
        """Resolved policy name for display (None if the policy was deleted). Relies
        on the caller eager-loading ``policy`` (selectinload) so no lazy query fires."""
        return self.policy.name if self.policy else None

