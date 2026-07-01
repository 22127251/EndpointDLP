import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import JSONB
from uuid6 import uuid7


class ViolationLog(Base):
    """One row per notable agent decision (a faithful projection of the agent's
    events.jsonl). An event is recorded iff it has policy matches OR a failure
    ``reason``; the matched policies live in the ``matches`` child rows. There is no
    single 'deciding' policy — the agent never names one."""
    __tablename__ = "violation_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(50), default="all")
    # Final decision for the event: "BLOCK" | "ALLOW".
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    # Machine category behind the outcome (mirrors events.jsonl `reason` / ECS
    # event.reason): policy_violation / oversize / text_cap / unsupported_format /
    # timeout / analysis_error / malformed. NULL for an allow_log-only event.
    reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Event metadata: {req_id, name, url, elapsed_ms}.
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())

    # Relationships
    agent = relationship("Agent")
    matches = relationship(
        "ViolationPolicyMatch",
        back_populates="violation",
        cascade="all, delete-orphan",
    )

    @property
    def agent_hostname(self) -> str | None:
        """Resolved agent hostname for display (relies on the caller eager-loading
        ``agent`` so no lazy query fires)."""
        return self.agent.hostname if self.agent else None
