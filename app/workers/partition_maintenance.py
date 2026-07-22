"""Scheduled job that keeps `events` monthly partitions ahead of the calendar."""

from __future__ import annotations

import structlog

from app.core.db import engine
from app.core.partitions import ensure_events_partitions

log = structlog.get_logger(__name__)


async def run_once() -> dict[str, list[str]]:
    """Ensure current and next month's `events` partitions exist."""
    async with engine.begin() as conn:
        ensured = await ensure_events_partitions(conn)
    log.info("partition_maintenance.ran", tables=ensured)
    return {"ensured": ensured}
