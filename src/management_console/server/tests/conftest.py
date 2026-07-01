"""Shared fixtures for the server test suite.

Runs against a dedicated ``dlpserver_test`` database on the compose Postgres
(localhost:5432). Each test gets a fresh schema (drop_all + create_all) so tests are
fully isolated; auth is bypassed via a dependency override returning a seeded admin.
"""
from __future__ import annotations

import os
import sys

import pytest_asyncio
import sqlalchemy as sa
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession,
)

# Make the server root importable (`app` package + `main`).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import Base, get_db                       # noqa: E402
from app.api.deps import get_current_user, is_admin_user     # noqa: E402
from app.models.user import User                             # noqa: E402
from app.schemas.user import UserRole                        # noqa: E402
from main import app                                         # noqa: E402

PG_HOST = os.environ.get("TEST_PG_HOST", "localhost")
PG_PORT = os.environ.get("TEST_PG_PORT", "5432")
PG_USER = os.environ.get("TEST_PG_USER", "postgres")
PG_PASS = os.environ.get("TEST_PG_PASS", "password")
TEST_DB_NAME = "dlpserver_test"

MAINT_URL = f"postgresql+asyncpg://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/postgres"
TEST_URL = f"postgresql+asyncpg://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"


async def _ensure_database(name: str) -> None:
    engine = create_async_engine(MAINT_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :n").bindparams(n=name)
            )
            if not exists:
                await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_engine():
    await _ensure_database(TEST_DB_NAME)
    engine = create_async_engine(TEST_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def sessionmaker(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def admin_user(sessionmaker) -> User:
    async with sessionmaker() as s:
        user = User(
            username="tester", email="tester@example.com",
            hashed_password="x", full_name="Tester",
            role=UserRole.ADMIN, is_active=True,
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


@pytest_asyncio.fixture
async def client(sessionmaker, admin_user):
    async def _get_db():
        async with sessionmaker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[is_admin_user] = lambda: admin_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def make_agent(client: AsyncClient, hostname: str = "vm-1") -> str:
    """Create an agent via the API and return its id."""
    r = await client.post("/api/v1/agents/register",
                          json={"hostname": hostname, "status": "inactive"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def make_policy(client: AsyncClient, name: str = "Visa") -> str:
    """Create a minimal score-ladder policy and return its id."""
    r = await client.post("/api/v1/policies/", json={
        "name": name, "type": "regex", "patterns": [r"\b4\d{3}\b"],
        "user_message": "Credit card number (Visa) detected",
        "context_words": ["visa"], "context_range": 120,
        "actions": [{"min_score": 1.0, "action": "block"},
                    {"min_score": 0.0, "action": "allow_log"}],
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]
