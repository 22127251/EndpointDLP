# app/models/alert.py
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    policy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("policies.id", ondelete="SET NULL"))
    severity: Mapped[str] = mapped_column(String(20), default="medium")
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(500))
    file_path: Mapped[str | None] = mapped_column(Text)
    matched_content: Mapped[str | None] = mapped_column(Text)
    action_taken: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="new")
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    agent = relationship("Agent", lazy="selectin")
    policy = relationship("Policy", lazy="selectin")
