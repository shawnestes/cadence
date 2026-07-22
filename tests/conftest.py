"""Test harness for Cadence.

Isolation strategy
------------------
All DB work runs on a single pytest-asyncio session-scoped event loop. Tests
use `httpx.AsyncClient` over `ASGITransport` (not FastAPI's sync `TestClient`)
so the app handlers and SQLAlchemy engine share that same loop — sync
`TestClient` spawns its own loop via anyio's blocking portal which makes the
asyncpg engine's pool reject futures with "attached to a different loop".

Rows created during a test must use emails ending in `@cadencetests.io`; the
autouse `_clean_test_rows` fixture deletes them before and after each test.

Tests run against the local Docker Postgres (docker compose -f
docker-compose.test.yml up -d --wait), NOT the shared Aurora dev DB. Set
CADENCE_TEST_DATABASE_URL to point somewhere else.
"""

from __future__ import annotations

import os

# Must run before any `app` import because get_settings() requires CADENCE_*.
# Real env vars take precedence over .env in pydantic-settings, so this
# redirects tests away from the Aurora URL in .env.
_TEST_DB_URL = os.environ.get(
    "CADENCE_TEST_DATABASE_URL",
    # 127.0.0.1, not localhost: on Windows, ::1 reaches Docker's proxy but the
    # handshake never completes, stalling every new connection ~25s before
    # asyncpg falls back to IPv4.
    "postgresql+asyncpg://cadence:cadence@127.0.0.1:5433/cadence_test",
)
os.environ["CADENCE_DATABASE_URL"] = _TEST_DB_URL
os.environ.setdefault("CADENCE_API_KEY", "test-api-key")
# Tests must never hit the real Anthropic API: real env vars take precedence
# over .env in pydantic-settings, so this forces the mock backend.
os.environ["CADENCE_AI_BACKEND"] = "mock"

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.db import async_session_factory, engine
from app.core.partitions import ensure_events_partitions
from app.main import app


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _bootstrap_test_db():
    """Bring the test DB to head + ensure event partitions.

    The docker-compose test DB is tmpfs-backed, so every container restart
    starts empty — the suite must be self-bootstrapping.
    """
    await asyncio.to_thread(_run_migrations)
    async with engine.begin() as conn:
        await ensure_events_partitions(conn)


@pytest.fixture(scope="session")
def api_key() -> str:
    return "test-api-key"


@pytest.fixture(scope="session")
def auth_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def db_session():
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


@pytest_asyncio.fixture(autouse=True)
async def _clean_test_rows(request: pytest.FixtureRequest):
    # Domain tests are pure logic — skip the DB purge round trips entirely.
    if "domain" in request.node.path.parts:
        yield
        return

    async def _purge() -> None:
        async with async_session_factory() as s:
            await s.execute(
                text(
                    "DELETE FROM events WHERE recipient_id IN "
                    "(SELECT id FROM recipients WHERE email LIKE :pat)"
                ),
                {"pat": "%@cadencetests.io"},
            )
            await s.execute(
                text(
                    "DELETE FROM sends WHERE recipient_id IN "
                    "(SELECT id FROM recipients WHERE email LIKE :pat)"
                ),
                {"pat": "%@cadencetests.io"},
            )
            await s.execute(
                text(
                    "DELETE FROM journeys WHERE recipient_id IN "
                    "(SELECT id FROM recipients WHERE email LIKE :pat)"
                ),
                {"pat": "%@cadencetests.io"},
            )
            await s.execute(
                text(
                    "DELETE FROM journeys WHERE workflow_id IN "
                    "(SELECT id FROM workflows WHERE name LIKE 'test-%')"
                )
            )
            await s.execute(
                text(
                    "DELETE FROM workflow_versions WHERE workflow_id IN "
                    "(SELECT id FROM workflows WHERE name LIKE 'test-%')"
                )
            )
            await s.execute(
                text(
                    "DELETE FROM templates WHERE campaign_id IN "
                    "(SELECT id FROM campaigns WHERE name LIKE 'test-%')"
                )
            )
            await s.execute(text("DELETE FROM campaigns WHERE name LIKE 'test-%'"))
            await s.execute(text("DELETE FROM workflows WHERE name LIKE 'test-%'"))
            await s.execute(
                text("DELETE FROM unsub_outbox WHERE email LIKE :pat"),
                {"pat": "%@cadencetests.io"},
            )
            await s.execute(
                text("DELETE FROM suppressions WHERE email LIKE :pat"),
                {"pat": "%@cadencetests.io"},
            )
            await s.execute(
                text("DELETE FROM recipients WHERE email LIKE :pat"),
                {"pat": "%@cadencetests.io"},
            )
            await s.execute(text("DELETE FROM alerts WHERE tenant = 'otto'"))
            await s.commit()

    await _purge()
    yield
    await _purge()
