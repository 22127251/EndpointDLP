import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime
from sqlalchemy.orm import Mapped, mapped_column
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
    status: Mapped[AgentStatus] = mapped_column(Enum(AgentStatus), default=AgentStatus.INACTIVE)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
