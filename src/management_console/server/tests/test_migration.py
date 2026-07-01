"""Phase 1 server test: the Alembic chain (incl. the new revision) round-trips.

Runs alembic up -> down -> up against a throwaway ``dlpserver_migtest`` database so it
exercises the real migration scripts (not just create_all). Skips cleanly if Postgres
is unreachable.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import PG_HOST, PG_PORT, PG_USER, PG_PASS, MAINT_URL

SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_DB = "dlpserver_migtest"
MIG_URL = f"postgresql+asyncpg://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{MIG_DB}"


async def _recreate_db(name: str) -> None:
    engine = create_async_engine(MAINT_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ).bindparams(n=name))
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
            await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


def _alembic(*args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=SERVER_DIR, env=env, capture_output=True, text=True,
    )


def test_migration_up_down_round_trip():
    try:
        asyncio.run(_recreate_db(MIG_DB))
    except Exception as exc:                                   # pragma: no cover
        pytest.skip(f"Postgres not reachable for migration test: {exc}")

    env = {**os.environ, "DATABASE_URL": MIG_URL}
    up = _alembic("upgrade", "head", env=env)
    assert up.returncode == 0, up.stderr
    down = _alembic("downgrade", "base", env=env)
    assert down.returncode == 0, down.stderr
    up2 = _alembic("upgrade", "head", env=env)
    assert up2.returncode == 0, up2.stderr
