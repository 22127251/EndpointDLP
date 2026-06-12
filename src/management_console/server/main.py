# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.api.v1.router import api_router
from app.config import get_settings
import asyncio
from app.services.heartbeat_service import offline_checker_loop
from app.services.seed import create_first_admin, create_init_settings
from app.database import async_session_maker

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("DLP Management Console is starting...")
    
    print("Checking for initial admin user...")
    # Create the initial admin user
    async with async_session_maker() as db:
        await create_first_admin(db)
        await create_init_settings(db)

    print("Heartbeat service is starting...")

    # Start the offline checker loop
    offline_checker_task = asyncio.create_task(offline_checker_loop())

    yield
    
    # Shutdown
    offline_checker_task.cancel()
    print("Heartbeat service to shut down...")
    print("Shutting down...")

app = FastAPI(
    title=settings.APP_NAME,
    description=settings.APP_DESCRIPTION,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)


# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
