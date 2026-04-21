from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from app.adapters.otto.client import OttoClient, OttoPushError
from app.core.db import async_session_factory
from app.models.unsub_outbox import UnsubOutbox

_BATCH_SIZE = 100
_MAX_ATTEMPTS = 10

log = structlog.get_logger(__name__)


async def run_once() -> dict[str, int]:
    client = OttoClient()
    delivered = 0
    failed = 0
    processed = 0

    async with async_session_factory() as session:
        stmt = (
            select(UnsubOutbox)
            .where(UnsubOutbox.tenant == "otto")
            .where(UnsubOutbox.delivered_at.is_(None))
            .where(UnsubOutbox.attempts < _MAX_ATTEMPTS)
            .order_by(UnsubOutbox.created_at.asc())
            .limit(_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

        for row in rows:
            processed += 1
            now = datetime.now(timezone.utc)
            try:
                await client.push_unsubscribe(row.email, row.reason, row.occurred_at)
            except OttoPushError as exc:
                row.attempts += 1
                row.last_attempt_at = now
                row.last_error = str(exc)[:500]
                failed += 1
                await session.commit()
                log.warning(
                    "otto_sync.push_failed",
                    email=row.email,
                    attempts=row.attempts,
                    error=row.last_error,
                )
            except Exception as exc:  # noqa: BLE001
                row.attempts += 1
                row.last_attempt_at = now
                row.last_error = str(exc)[:500]
                failed += 1
                await session.commit()
                log.exception(
                    "otto_sync.push_unexpected_error",
                    email=row.email,
                    attempts=row.attempts,
                )
            else:
                row.delivered_at = now
                row.attempts += 1
                row.last_attempt_at = now
                delivered += 1
                await session.commit()
                log.info(
                    "otto_sync.push_delivered",
                    email=row.email,
                    attempts=row.attempts,
                )

    return {"delivered": delivered, "failed": failed, "processed": processed}
