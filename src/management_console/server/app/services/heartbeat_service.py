# backend/app/services/offline_checker.py
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, update
from app.database import async_session_maker
from app.models.agent import Agent, AgentStatus
from app.models.setting import ServerConfiguration



async def offline_checker_loop():

    while True:
        offline_interval = 60
        try:
            async with async_session_maker() as session:
                config_result = await session.execute(select(ServerConfiguration).where(ServerConfiguration.id == 1))
                config = config_result.scalar_one_or_none()
                if config and config.settings:
                    offline_interval = config.settings.get("OFFLINE_SCAN_INTERVAL_SECONDS", 60)
                
                await check_and_mark_offline(session, config)
        except Exception as e:
            print(f"[OfflineChecker] Error: {e}")

        offline_interval = max(offline_interval, 30)
        await asyncio.sleep(offline_interval)


async def check_and_mark_offline(session, config):
    
    try:
        heartbeat_seconds = config.settings.get("HEARTBEAT_INTERVAL_SECONDS", 300) if config and config.settings else 300
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=heartbeat_seconds)

        result = await session.execute(
            update(Agent)
            .where(
                Agent.status == AgentStatus.ACTIVE,
                Agent.last_seen < cutoff
            )
            .values(status=AgentStatus.OFFLINE)
        )
        await session.commit()

        if result.rowcount > 0:
            print(f"[OfflineChecker] Marked {result.rowcount} agents as offline")

    except Exception:
        await session.rollback()
        raise