import uuid
from enum import StrEnum
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, Text, Enum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base
from sqlalchemy import func
from app.schemas.policy import PolicyAction, PolicyChanel, RuleType


    
class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    rule_type: Mapped[RuleType] = mapped_column(Enum(RuleType, name="rule_type"), nullable=False)
    rule: Mapped[dict] = mapped_column(JSONB, nullable=False)
    action: Mapped[PolicyAction] = mapped_column(Enum(PolicyAction, name="policy_action"), nullable=False)
    channel: Mapped[PolicyChanel] = mapped_column(Enum(PolicyChanel, name="policy_channel"), default=PolicyChanel.ALL )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )