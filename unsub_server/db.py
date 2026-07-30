"""Raw-SQL data layer. SQLAlchemy core over asyncpg; no ORM, no alembic --
this service never owns or migrates schema. Column shapes are cadence's;
drift is caught by tests/test_unsub_server_db.py in cadence's suite."""

from __future__ import annotations

import os
import uuid

import uuid_utils
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_engine: AsyncEngine | None = None


def _uuid7() -> uuid.UUID:
    return uuid.UUID(bytes=uuid_utils.uuid7().bytes)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = os.environ["UNSUB_DATABASE_URL"]
        _engine = create_async_engine(url, pool_size=5, max_overflow=5)
    return _engine


def reset_engine() -> None:
    global _engine
    _engine = None


async def ping() -> bool:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def fetch_recipient(tenant: str, recipient_id: str) -> dict | None:
    try:
        rid = uuid.UUID(recipient_id)
    except ValueError:
        return None
    async with get_engine().connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, email, status FROM recipients "
                    "WHERE tenant = :t AND id = :id"
                ),
                {"t": tenant, "id": rid},
            )
        ).mappings().first()
    return dict(row) if row is not None else None


async def insert_diagnostic(
    tenant: str | None,
    recipient_id: str | None,
    reason: str,
    token: str | None,
    host: str | None,
) -> None:
    """Record a failed unsubscribe lookup for later diagnosis.

    Users never see these as errors; the row is the only trace. recipient_id
    is stored as text-cast-to-uuid only when it parses; otherwise NULL (the
    raw value is still inside the token column).
    """
    rid: uuid.UUID | None
    try:
        rid = uuid.UUID(recipient_id) if recipient_id else None
    except ValueError:
        rid = None
    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO unsub_diagnostics (id, tenant, recipient_id, reason, token, host) "
                "VALUES (:id, :t, :rid, :reason, :token, :host)"
            ),
            {
                "id": _uuid7(),
                "t": tenant,
                "rid": rid,
                "reason": reason,
                "token": (token or "")[:1024] or None,
                "host": host,
            },
        )


async def record_unsub(tenant: str, recipient_id: str, email: str) -> None:
    """Suppress + queue outbox + flip recipient status, atomically.

    Mirrors cadence's POST /u/{token} (app/api/unsub.py): suppression is
    idempotent on (tenant, email); the outbox insert is skipped when an
    undelivered row for the same tenant+email exists from the last hour.
    """
    rid = uuid.UUID(recipient_id)
    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO suppressions (id, tenant, email, reason, source) "
                "VALUES (:id, :t, :e, 'unsubscribe', 'one_click') "
                "ON CONFLICT (tenant, email) DO NOTHING"
            ),
            {"id": _uuid7(), "t": tenant, "e": email},
        )
        await conn.execute(
            text(
                "INSERT INTO unsub_outbox (id, tenant, email, reason, occurred_at) "
                "SELECT :id, :t, :e, 'unsubscribe', now() "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM unsub_outbox "
                "  WHERE tenant = :t AND email = :e "
                "    AND delivered_at IS NULL "
                "    AND created_at >= now() - interval '1 hour'"
                ")"
            ),
            {"id": _uuid7(), "t": tenant, "e": email},
        )
        await conn.execute(
            text("UPDATE recipients SET status = 'suppressed', updated_at = now() WHERE id = :id"),
            {"id": rid},
        )
