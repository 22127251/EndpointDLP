from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # App Info
    APP_NAME: str = "DLP Management Console"
    APP_VERSION: str = "1.0.0"
    APP_DESCRIPTION: str = ""
    DEBUG: bool = False
    # Admin account
    INITIAL_ADMIN_USERNAME: str
    INITIAL_ADMIN_PASSWORD: str
    INITIAL_ADMIN_EMAIL: str = "admin@example.com"
    # Database
    DATABASE_URL: str
    # Pagination
    DEFAULT_PAGE_SIZE: int = 10
    MAX_PAGE_SIZE: int = 100

    # CORS
    CORS_ORIGINS: list[str] = ["*"]

    # JWT Auth
    SECRET_KEY: str
    ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480  # 8 hours
    AGENT_SECRET_KEY: str
    
    # server settings
    HEARTBEAT_INTERVAL_SECONDS: int = 60
    OFFLINE_SCAN_INTERVAL_SECONDS: int = 60
    AUTO_CLEAN_UP_LOG: bool = False
    LOG_RETENTION_DAYS: int = 30

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()