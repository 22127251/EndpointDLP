# backend/app/services/offline_checker.py
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import update
from app.database import async_session_maker
from app.config import get_settings
from app.models.agent import Agent, AgentStatus

settings = get_settings()

async def offline_checker_loop():
    while True:
        try:
            await check_and_mark_offline()
        except Exception as e:
            print(f"[OfflineChecker] Error: {e}")

        await asyncio.sleep(60)


async def check_and_mark_offline():
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.HEARTBEAT_INTERVAL_SECONDS
    )

    async with async_session_maker() as session:
        try:
            result = await session.execute(
                update(Agent)
                .where(
                    Agent.status    == AgentStatus.ACTIVE,
                    Agent.last_seen <  cutoff
                )
                .values(status=AgentStatus.OFFLINE)
            )
            await session.commit()

            if result.rowcount > 0:
                print(f"[OfflineChecker] Marked {result.rowcount} agents as offline")

        except Exception:
            await session.rollback()
            raise