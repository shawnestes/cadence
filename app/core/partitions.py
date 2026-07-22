"""Automatic monthly partition management for the `events` table.

`events` is `PARTITION BY RANGE (occurred_at)` (see alembic revision
4a2c9f3b1e0d). Partitions are not created automatically by Postgres, so
without maintenance, inserts for months beyond the last-created partition
fail with "no partition of relation events found for row". This module
provides idempotent DDL to keep the current and next month's partitions
present; call it at process startup and on a recurring schedule.
"""

from __future__ import annotations

from datetime import date

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["ensure_events_partitions"]

log = structlog.get_logger(__name__)


def _month_start(d: date) -> date:
    """Return the first day of the month containing `d`."""
    return d.replace(day=1)


def _add_month(d: date) -> date:
    """Return the first-of-month date one month after `d` (which must be first-of-month)."""
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1)
    return d.replace(month=d.month + 1)


def _target_months(today: date) -> list[date]:
    """Return the first-of-month dates for the current and next month."""
    this_month = _month_start(today)
    return [this_month, _add_month(this_month)]


async def ensure_events_partitions(
    conn: AsyncConnection, today: date | None = None
) -> list[str]:
    """Idempotently create monthly `events` partitions for the current and next month.

    Uses `CREATE TABLE IF NOT EXISTS ... PARTITION OF events`, which is a
    no-op when the partition already exists, so this is safe to call on
    every startup and on a recurring schedule.

    Args:
        conn: An open async connection with DDL privileges on `events`.
        today: Override for the current date (used in tests). Defaults to
            `date.today()`.

    Returns:
        Names of the partition tables that were ensured to exist.
    """
    today = today or date.today()
    ensured: list[str] = []
    for start in _target_months(today):
        end = _add_month(start)
        name = f"events_{start:%Y_%m}"
        await conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF events "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            )
        )
        ensured.append(name)
    log.info("partitions.ensured", tables=ensured)
    return ensured
