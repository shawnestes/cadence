"""Reputation checks worker: complaint_rate (from provider_daily_rollup) and
cold_pool (from segment_tier_snapshot + contact_profile) monitors.

Separate from reputation_monitor.py, which reads the `events` table via
_compute_topline. This worker reads the segmentation-layer rollup tables.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.alerts import AlertDispatcher, get_alert_dispatcher
from app.core.config import get_settings
from app.core.db import async_session_factory
from app.workers.reputation_monitor import _apply_alert_state

log = structlog.get_logger(__name__)


async def check_complaint_rate(
    session: AsyncSession,
    tenant: str,
    dispatcher: AlertDispatcher,
    now: datetime,
) -> None:
    """Read complaint_rate_7d from provider_daily_rollup for yesterday (source=1)
    and fire alerts per (reputation_group, channel) pair.
    """
    s = get_settings()

    rows_stmt = text(
        """
        SELECT reputation_group, channel, complaint_rate_7d
        FROM provider_daily_rollup
        WHERE tenant = :tenant
          AND day = CURRENT_DATE - 1
          AND source = 1
        """
    )
    result = await session.execute(rows_stmt, {"tenant": tenant})
    rows = result.fetchall()

    if not rows:
        log.info("reputation_checks.missing_rollup_data", tenant=tenant, day="yesterday")
        return

    for row in rows:
        reputation_group = row[0]
        channel = row[1]
        complaint_rate_7d = float(row[2]) if row[2] is not None else 0.0

        metric_key = f"complaint_rate:rg{reputation_group}:ch{channel}"

        await _apply_alert_state(
            session,
            tenant,
            metric_key,
            complaint_rate_7d,
            s.reputation_complaint_rate_warn,
            s.reputation_complaint_rate_critical,
            dispatcher,
            now,
        )


async def check_cold_pool(
    session: AsyncSession,
    tenant: str,
    dispatcher: AlertDispatcher,
    now: datetime,
) -> None:
    """Read the last N runs from segment_tier_snapshot and check for consecutive
    net-negative cold-pool trends. Also reads contact_profile for current absolute
    cold count.
    """
    s = get_settings()
    n_runs = s.reputation_cold_pool_net_negative_runs

    # Query the last n_runs distinct run_at timestamps, then pivot tier counts.
    # segment_tier_snapshot schema: (id, tenant, run_at, tier SMALLINT, contact_count INT)
    # TIER_COLD = 5, TIER_NEW = 6
    snapshot_stmt = text(
        """
        SELECT
            run_at,
            SUM(contact_count) FILTER (WHERE tier = 5) AS cold_count,
            SUM(contact_count) FILTER (WHERE tier = 6) AS acquisition_count
        FROM segment_tier_snapshot
        WHERE tenant = :tenant
          AND run_at IN (
              SELECT DISTINCT run_at
              FROM segment_tier_snapshot
              WHERE tenant = :tenant
              ORDER BY run_at DESC
              LIMIT :n_runs
          )
        GROUP BY run_at
        ORDER BY run_at DESC
        """
    )
    result = await session.execute(snapshot_stmt, {"tenant": tenant, "n_runs": n_runs})
    snapshots = result.fetchall()

    if len(snapshots) < 2:
        log.info(
            "reputation_checks.cold_pool_insufficient_snapshots",
            tenant=tenant,
            found=len(snapshots),
            needed=n_runs,
        )
        return

    # Check for consecutive net-negative runs.
    # Net negative: cold_count_delta > acquisition_delta for a run.
    # We compare successive rows (sorted DESC means snapshots[0] is newest).
    # Row columns: (run_at, cold_count, acquisition_count)
    consecutive_net_negative = 0
    for i in range(len(snapshots) - 1):
        newer = snapshots[i]
        older = snapshots[i + 1]
        cold_count_delta = (newer[1] or 0) - (older[1] or 0)
        acquisition_delta = (newer[2] or 0) - (older[2] or 0)
        if cold_count_delta > acquisition_delta:
            consecutive_net_negative += 1
        else:
            break

    if consecutive_net_negative < n_runs - 1:
        log.debug(
            "reputation_checks.cold_pool_ok",
            tenant=tenant,
            consecutive_net_negative=consecutive_net_negative,
        )
        return

    # Read current absolute cold count from contact_profile.
    # engagement_tier is SMALLINT; TIER_COLD = 5.
    cold_count_stmt = text(
        """
        SELECT COUNT(*)
        FROM contact_profile
        WHERE tenant = :tenant
          AND engagement_tier = 5
        """
    )
    cold_result = await session.execute(cold_count_stmt, {"tenant": tenant})
    absolute_cold_count = cold_result.scalar() or 0

    log.warning(
        "reputation_checks.cold_pool_net_negative",
        tenant=tenant,
        consecutive_runs=consecutive_net_negative,
        absolute_cold_count=absolute_cold_count,
    )

    await _apply_alert_state(
        session,
        tenant,
        "cold_pool",
        float(consecutive_net_negative),
        float(n_runs - 1),
        float(n_runs - 1),  # no critical level; warn threshold == critical threshold
        dispatcher,
        now,
    )


async def run_once(dispatcher: AlertDispatcher | None = None) -> None:
    s = get_settings()
    if dispatcher is None:
        dispatcher = get_alert_dispatcher()

    now = datetime.now(timezone.utc)

    for tenant in s.alert_tenants:
        try:
            async with async_session_factory() as session:
                await check_complaint_rate(session, tenant, dispatcher, now)
        except Exception:  # noqa: BLE001
            log.exception(
                "reputation_checks.error",
                tenant=tenant,
                check="complaint_rate",
            )

        try:
            async with async_session_factory() as session:
                await check_cold_pool(session, tenant, dispatcher, now)
        except Exception:  # noqa: BLE001
            log.exception(
                "reputation_checks.error",
                tenant=tenant,
                check="cold_pool",
            )
