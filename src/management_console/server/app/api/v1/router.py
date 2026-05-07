# app/api/v1/router.py
from fastapi import APIRouter
from app.api.v1 import policies
from app.api.v1 import agents, auth, agent_groups, metadata, violation_logs, settings

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(policies.router)
api_router.include_router(agents.router)
api_router.include_router(agent_groups.router)
api_router.include_router(metadata.router)
api_router.include_router(violation_logs.router)
api_router.include_router(settings.router)

# api_router.include_router(dashboard.router)
