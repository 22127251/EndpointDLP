# app/config.py
from pydantic_settings import BaseSettings
from functools import lru_cache
from dotenv import load_dotenv


class Settings(BaseSettings):
    APP_NAME: str = "DLP Management Console"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://dlp_user:password@localhost:5432/dlp_db"

    # JWT Auth
    SECRET_KEY: str = "your-super-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480  # 8 hours

    # Pagination
    DEFAULT_PAGE_SIZE: int = 20
    MAX_PAGE_SIZE: int = 100

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
