import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, Text, BigInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
from sqlalchemy import func
from uuid6 import uuid7


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE")
    )
    log_type: Mapped[str] = mapped_column(String(50))  # "events" | "agent_log"
    content: Mapped[str] = mapped_column(Text)
    byte_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    agent = relationship("Agent")
