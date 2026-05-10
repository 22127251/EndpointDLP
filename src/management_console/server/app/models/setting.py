from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB
from app.database import Base

class ServerConfiguration(Base):
    __tablename__ = "server_configurations"
    
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    settings: Mapped[dict] = mapped_column(JSONB, default={
        "VIOLATION_LOG_RETENTION_DAYS": 90,
        "HEARTBEAT_INTERVAL_SECONDS": 60,
    })