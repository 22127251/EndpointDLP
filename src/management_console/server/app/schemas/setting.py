from pydantic import BaseModel, Field
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB
from app.database import Base

class ServerConfigurationUpdate(BaseModel):
    settings: dict = Field(
        ...,
        examples=[
            {
                "VIOLATION_LOG_RETENTION_DAYS": 90,
                "HEARTBEAT_INTERVAL_SECONDS": 60
            }
        ]
    )