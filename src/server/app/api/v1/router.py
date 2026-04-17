# app/api/v1/router.py
from fastapi import APIRouter
from app.api.v1 import auth, policies, alerts, agents, dashboard

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(policies.router)
api_router.include_router(alerts.router)
api_router.include_router(agents.router)
api_router.include_router(dashboard.router)
