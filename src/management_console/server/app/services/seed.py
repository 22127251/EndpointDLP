from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.schemas.user import UserRole
from app.config import get_settings
from app.utils.security import hash_password
from app.models.setting import ServerConfiguration

settings = get_settings()

async def create_first_admin(db: AsyncSession):
    result = await db.execute(select(User).limit(1))
    user_exists = result.scalar_one_or_none()

    if not user_exists:
        print("Creating initial admin user...")
        new_admin = User(
            username=settings.INITIAL_ADMIN_USERNAME,
            email=settings.INITIAL_ADMIN_EMAIL,
            hashed_password=hash_password(settings.INITIAL_ADMIN_PASSWORD),
            full_name="System Administrator",
            role=UserRole.ADMIN,
            is_active=True
        )
        db.add(new_admin)
        await db.commit()
        print(f"Admin user '{settings.INITIAL_ADMIN_USERNAME}' created successfully.")
    else:
        print("Admin user already exists. Skipping initialization.")

async def create_init_settings(db: AsyncSession):
    # Check if settings already exist
    result = await db.execute(select(ServerConfiguration).limit(1))
    settings_exists = result.scalar_one_or_none()

    if not settings_exists:
        print("Creating initial server configuration...")
        new_settings = ServerConfiguration(
            settings={
                "HEARTBEAT_INTERVAL_SECONDS": settings.HEARTBEAT_INTERVAL_SECONDS,
                "OFFLINE_SCAN_INTERVAL_SECONDS": settings.OFFLINE_SCAN_INTERVAL_SECONDS,
                "AUTO_CLEAN_UP_VIOLATION_LOG": settings.AUTO_CLEAN_UP_VIOLATION_LOG,
                "VIOLATION_LOG_RETENTION_DAYS": settings.VIOLATION_LOG_RETENTION_DAYS
            }
        )
        db.add(new_settings)
        await db.commit()
        print("Initial server configuration created successfully.")
    else:
        print("Server configuration already exists. Skipping initialization.")