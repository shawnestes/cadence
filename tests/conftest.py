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
"""

from __future__ import annotations

import os

# Must run before any `app` import because get_settings() requires CADENCE_*.
# CADENCE_DATABASE_URL comes from .env (real Aurora) — tests use the live cadence DB.
os.environ.setdefault("CADENCE_API_KEY", "test-api-key")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.db import async_session_factory
from app.main import app


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
async def _clean_test_rows():
    async def _purge() -> None:
        async with async_session_factory() as s:
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
            await s.commit()

    await _purge()
    yield
    await _purge()
