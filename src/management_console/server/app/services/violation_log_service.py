import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete, select
from app.database import async_session_maker
from app.models.violation_log import ViolationLog
from app.models.setting import ServerConfiguration


async def run_log_cleanup():
    async with async_session_maker() as session:
        try:
            result = await session.execute(
                select(ServerConfiguration).where(ServerConfiguration.id == 1)
            )
            config = result.scalar_one_or_none()

            if not config or not config.settings:
                print("[LogCleanup] There is no configuration for log cleanup. Skipping.")
                return

            is_enabled = config.settings.get("AUTO_CLEAN_UP_VIOLATION_LOG", False)
            retention_days = config.settings.get("VIOLATION_LOG_RETENTION_DAYS", 90)

            if not is_enabled:
                print("[LogCleanup] Auto log cleanup is disabled. Skipping.")
                return

          
            threshold_date = datetime.now(timezone.utc) - timedelta(days=int(retention_days))
            
            result = await session.execute(
                delete(ViolationLog)
                .where(ViolationLog.created_at < threshold_date)
            )
            await session.commit()

            
            print(f"[LogCleanup] Deleted {result.rowcount} older than {retention_days} days.")
            

        except Exception as e:
            await session.rollback()
            print(f"[LogCleanup] Error occurred while cleaning up logs: {e}")

async def log_cleanup_loop():
    print("[LogCleanup] Auto cleanup log task started.")
    while True:
        await run_log_cleanup()
        await asyncio.sleep(3600)