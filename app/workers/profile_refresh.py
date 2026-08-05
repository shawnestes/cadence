"""Incremental and nightly profile refresh worker (SPEC-segmentation.md §5).

Two entry points:
- run_incremental(): called every 120 s; recomputes only dirty profiles
  (profile_version > watermark). Skips if already running.
- run_nightly(): called at 03:30 cron; full recompute of all profiles with
  blast-radius guardrail before committing tier changes (D4).

Set-based SQL throughout: no per-contact Python loops for the aggregation
step. Tier/score computation requires a Python pass (pure domain functions),
which is acceptable at ≤50 k rows per incremental batch.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import text

from app.core.config import get_engagement_config, get_settings
from app.core.db import async_session_factory
from app.domain.engagement_score import compute_score
from app.domain.engagement_v2 import classify_tier
from app.services.segment_loader import load_segments
from app.services.segment_query import compile_segment

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level watermark state
# ---------------------------------------------------------------------------

_lock: asyncio.Lock = asyncio.Lock()
_watermark: int = 0

_INCREMENTAL_LIMIT = 50_000

# ---------------------------------------------------------------------------
# Shared aggregation SQL fragments
# ---------------------------------------------------------------------------

# Recompute window counters for a set of (tenant, contact_id, channel) tuples
# passed as a CTE named `dirty`. Used by both incremental and nightly passes.
_WINDOW_UPDATE_SQL = """
WITH dirty AS (
    {dirty_cte}
),
agg AS (
    SELECT
        cad.contact_id,
        cad.channel,
        cad.tenant,
        SUM(cad.sends)      FILTER (WHERE cad.day >= CURRENT_DATE - 30)  AS sends_30d,
        SUM(cad.opens)      FILTER (WHERE cad.day >= CURRENT_DATE - 30)  AS opens_30d,
        SUM(cad.clicks)     FILTER (WHERE cad.day >= CURRENT_DATE - 30)  AS clicks_30d,
        SUM(cad.sends)      FILTER (WHERE cad.day >= CURRENT_DATE - 90)  AS sends_90d,
        SUM(cad.opens)      FILTER (WHERE cad.day >= CURRENT_DATE - 90)  AS opens_90d,
        SUM(cad.clicks)     FILTER (WHERE cad.day >= CURRENT_DATE - 90)  AS clicks_90d,
        SUM(cad.converts)   FILTER (WHERE cad.day >= CURRENT_DATE - 90)  AS converts_90d,
        SUM(cad.bounces)    FILTER (WHERE cad.day >= CURRENT_DATE - 90)  AS bounces_90d,
        SUM(cad.complaints) FILTER (WHERE cad.day >= CURRENT_DATE - 90)  AS complaints_90d,
        SUM(cad.sends)      AS sends_lifetime,
        SUM(cad.opens)      AS opens_lifetime,
        SUM(cad.clicks)     AS clicks_lifetime,
        MIN(CASE WHEN cad.sends > 0 THEN cad.day END) AS first_send_at,
        MAX(CASE WHEN cad.sends > 0 THEN cad.day END) AS last_send_at
    FROM contact_activity_daily cad
    WHERE (cad.tenant, cad.contact_id, cad.channel)
          IN (SELECT tenant, contact_id, channel FROM dirty)
    GROUP BY cad.contact_id, cad.channel, cad.tenant
)
UPDATE contact_profile cp
SET
    sends_30d      = COALESCE(agg.sends_30d,      0),
    opens_30d      = COALESCE(agg.opens_30d,      0),
    clicks_30d     = COALESCE(agg.clicks_30d,     0),
    sends_90d      = COALESCE(agg.sends_90d,      0),
    opens_90d      = COALESCE(agg.opens_90d,      0),
    clicks_90d     = COALESCE(agg.clicks_90d,     0),
    converts_90d   = COALESCE(agg.converts_90d,   0),
    bounces_90d    = COALESCE(agg.bounces_90d,    0),
    complaints_90d = COALESCE(agg.complaints_90d, 0),
    sends_lifetime = COALESCE(agg.sends_lifetime,  0),
    opens_lifetime = COALESCE(agg.opens_lifetime,  0),
    clicks_lifetime= COALESCE(agg.clicks_lifetime, 0),
    first_send_at  = agg.first_send_at,
    last_send_at   = agg.last_send_at,
    updated_at     = now()
FROM agg
WHERE cp.contact_id = agg.contact_id
  AND cp.channel    = agg.channel
  AND cp.tenant     = agg.tenant
"""

_DIRTY_CTE_INCREMENTAL = (
    "SELECT tenant, contact_id, channel FROM contact_profile "
    "WHERE profile_version > :watermark "
    "ORDER BY profile_version LIMIT :limit"
)

_DIRTY_CTE_NIGHTLY = (
    "SELECT tenant, contact_id, channel FROM contact_profile"
)


async def _fetch_profiles_for_tier(
    conn: Any,
    dirty_cte: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fetch updated profile fields needed to compute tier/score."""
    sql = text(
        f"""
        WITH dirty AS (
            {dirty_cte}
        )
        SELECT
            cp.tenant,
            cp.contact_id,
            cp.channel,
            cp.mailbox_provider,
            cp.clicks_90d,
            cp.converts_90d,
            cp.opens_30d,
            cp.opens_90d,
            cp.sends_90d,
            cp.last_open_at,
            cp.last_click_at,
            cp.last_convert_at,
            cp.sends_lifetime,
            cp.first_send_at,
            cp.bounces_90d,
            cp.complaints_90d
        FROM contact_profile cp
        JOIN dirty d
          ON  cp.tenant     = d.tenant
          AND cp.contact_id = d.contact_id
          AND cp.channel    = d.channel
        """
    )
    result = await conn.execute(sql, params)
    return [dict(row._mapping) for row in result]


def _compute_tier_and_score(
    rows: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Return list of {tenant, contact_id, channel, tier, score} dicts."""
    config = get_engagement_config()
    updates: list[dict[str, Any]] = []

    for row in rows:
        tier = classify_tier(
            clicks_long=row["clicks_90d"] or 0,
            converts_long=row["converts_90d"] or 0,
            opens_short=row["opens_30d"] or 0,
            opens_long=row["opens_90d"] or 0,
            sends_long=row["sends_90d"] or 0,
            last_open_at=row["last_open_at"],
            sends_lifetime=row["sends_lifetime"] or 0,
            first_send_at=row["first_send_at"],
            mailbox_provider=row["mailbox_provider"],
            config=config,
            now=now,
        )
        score = compute_score(
            last_click_at=row["last_click_at"],
            last_open_at=row["last_open_at"],
            last_convert_at=row["last_convert_at"],
            complaints_90d=row["complaints_90d"] or 0,
            bounces_90d=row["bounces_90d"] or 0,
            config=config.score,
            now=now,
        )
        updates.append(
            {
                "p_tenant": row["tenant"],
                "p_contact_id": str(row["contact_id"]),
                "p_channel": row["channel"],
                "p_tier": tier,
                "p_score": score,
            }
        )

    return updates


async def _apply_tier_score_updates(
    conn: Any,
    updates: list[dict[str, Any]],
    tier_config_version: str,
) -> None:
    """Bulk-UPDATE engagement_tier and engagement_score for the dirty set."""
    if not updates:
        return

    # Parameterized per-row UPDATEs — avoids f-string interpolation of
    # potentially-external data (tenant, contact_id, channel, tier, score).
    update_sql = text(
        """
        UPDATE contact_profile
        SET
            engagement_tier     = :tier,
            engagement_score    = :score,
            tier_config_version = :tier_config_version,
            updated_at          = now()
        WHERE tenant     = :tenant
          AND contact_id = :contact_id
          AND channel    = :channel
        """
    )
    for u in updates:
        await conn.execute(
            update_sql,
            {
                "tenant": u["p_tenant"],
                "contact_id": u["p_contact_id"],
                "channel": u["p_channel"],
                "tier": u["p_tier"],
                "score": u["p_score"],
                "tier_config_version": tier_config_version,
            },
        )


# ---------------------------------------------------------------------------
# Deferred rule-1 resolver
# ---------------------------------------------------------------------------

_RULE1_LIMIT = 1_000

# event_type = 7 is the click event code (SES_EVENT_TYPE_CODES["Click"]).
# source = 1 = SES, source = 2 = origin_platform.
_SELECT_PENDING_CLICKS_SQL = """
SELECT
    cr.click_id,
    cr.contact_id,
    cr.channel,
    cr.tenant,
    cr.clicked_at::date AS click_day
FROM click_registry cr
WHERE cr.classification = 0
  AND cr.reclassified_at IS NULL
  AND cr.clicked_at < now() - (:window_s || ' seconds')::interval
  AND NOT EXISTS (
      SELECT 1
      FROM contact_event ce
      WHERE ce.click_id   = cr.click_id
        AND ce.event_type = 7
        AND ce.source     = 2
  )
LIMIT :limit
"""

_UPDATE_CLICK_REGISTRY_SQL = """
UPDATE click_registry
SET classification  = 2,
    reclassified_at = now()
WHERE click_id = :click_id
"""

_UPDATE_CONTACT_EVENT_SQL = """
UPDATE contact_event
SET classification = 2
WHERE click_id   = :click_id
  AND event_type = 7
  AND source     = 1
"""

_UPDATE_ACTIVITY_DAILY_SQL = """
UPDATE contact_activity_daily
SET clicks     = clicks - 1,
    clicks_bot = clicks_bot + 1
WHERE contact_id = :contact_id
  AND channel    = :channel
  AND tenant     = :tenant
  AND day        = :click_day
  AND clicks     > 0
"""

_BUMP_PROFILE_VERSION_SQL = """
UPDATE contact_profile
SET profile_version = profile_version + 1,
    updated_at      = now()
WHERE contact_id = :contact_id
  AND channel    = :channel
  AND tenant     = :tenant
"""


async def _resolve_pending_clicks(conn: Any) -> int:
    """Reclassify expired pending clicks (rule 1: no origin-platform confirmation).

    Selects click_registry rows where classification=0, reclassified_at IS NULL,
    and clicked_at is older than scanner_confirm_window_s.  Any row that still
    has no origin-platform contact_event (source=2, event_type=7) is reclassified
    as scanner (classification=2) and the corresponding counters are corrected.

    Returns the number of clicks reclassified.
    """
    settings = get_settings()
    window_s = settings.scanner_confirm_window_s

    pending_result = await conn.execute(
        text(_SELECT_PENDING_CLICKS_SQL),
        {"window_s": str(window_s), "limit": _RULE1_LIMIT},
    )
    pending_rows = pending_result.fetchall()

    if not pending_rows:
        return 0

    for row in pending_rows:
        await conn.execute(
            text(_UPDATE_CLICK_REGISTRY_SQL),
            {"click_id": row.click_id},
        )
        await conn.execute(
            text(_UPDATE_CONTACT_EVENT_SQL),
            {"click_id": row.click_id},
        )
        await conn.execute(
            text(_UPDATE_ACTIVITY_DAILY_SQL),
            {
                "contact_id": str(row.contact_id),
                "channel": row.channel,
                "tenant": row.tenant,
                "click_day": row.click_day,
            },
        )
        await conn.execute(
            text(_BUMP_PROFILE_VERSION_SQL),
            {
                "contact_id": str(row.contact_id),
                "channel": row.channel,
                "tenant": row.tenant,
            },
        )

    return len(pending_rows)


# ---------------------------------------------------------------------------
# Segment membership materialization
# ---------------------------------------------------------------------------


async def _materialize_segments(
    conn: Any,
    dirty_contact_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Materialize segment membership for all active segment_definition rows.

    For each active segment, computes the current matching set of contacts
    and diffs against the existing active members in segment_member.
    New members are inserted; removed members have removed_at set.

    Also updates segment_definition.excluded_by_guard_count with the number
    of cold contacts excluded by the cold-exclusion guard.

    Args:
        conn: Active async SQLAlchemy session/connection (inside a transaction).
        dirty_contact_ids: When provided (incremental pass), only contacts in
            this list are evaluated for membership additions. Removals due to
            window decay are caught by the nightly pass.

    Returns:
        Summary dict: {"segments_processed": int, "total_added": int, "total_removed": int}.
    """
    config = get_engagement_config()
    now = datetime.now(timezone.utc)

    # Fetch all active segment definitions.
    sd_result = await conn.execute(
        text(
            "SELECT segment_id, tenant, key, channel, predicate, spec_hash, "
            "acknowledges_cold, include_unsendable "
            "FROM segment_definition WHERE active = true"
        )
    )
    segment_defs = sd_result.fetchall()

    total_added = 0
    total_removed = 0

    for sd in segment_defs:
        seg_id = str(sd.segment_id)
        predicate = sd.predicate  # Already deserialized from jsonb by the driver.

        # Compile the guarded WHERE clause for this segment.
        where_sql, params = compile_segment(
            predicate,
            config,
            now=now,
            acknowledges_cold=sd.acknowledges_cold,
            include_unsendable=sd.include_unsendable,
        )

        # Build params dict for current-member query.
        query_params: dict[str, Any] = dict(params) | {"tenant": sd.tenant}

        # The effective WHERE clause for the guarded query (pre-dirty-narrowing).
        guarded_where_sql = where_sql

        # Optionally narrow to dirty contacts only (incremental pass).
        if dirty_contact_ids is not None:
            where_sql = f"({where_sql}) AND cp.contact_id = ANY(:dirty_ids)"
            query_params["dirty_ids"] = dirty_contact_ids

        # Fetch current matching contact IDs.
        current_sql = text(
            f"SELECT cp.contact_id FROM contact_profile cp "
            f"WHERE cp.tenant = :tenant AND {where_sql}"
        )
        current_result = await conn.execute(current_sql, query_params)
        current_ids: set[str] = {str(row.contact_id) for row in current_result}

        # Fetch previously-active member contact IDs.
        prev_result = await conn.execute(
            text(
                "SELECT contact_id FROM segment_member "
                "WHERE segment_id = :seg_id AND removed_at IS NULL"
            ),
            {"seg_id": seg_id},
        )
        previous_ids: set[str] = {str(row.contact_id) for row in prev_result}

        # Determine additions and removals (scoped to dirty set for incremental).
        if dirty_contact_ids is not None:
            dirty_set = set(dirty_contact_ids)
            new_ids = current_ids - (previous_ids & dirty_set)
            # Only remove contacts that are in the dirty set and no longer match.
            removed_ids = (previous_ids & dirty_set) - current_ids
        else:
            new_ids = current_ids - previous_ids
            removed_ids = previous_ids - current_ids

        # Insert new members using a single VALUES-list INSERT ... SELECT.
        if new_ids:
            # Build a parameterized values list to avoid per-row round-trips.
            values_clauses: list[str] = []
            insert_params: dict[str, Any] = {
                "seg_id": seg_id,
                "tenant": sd.tenant,
                "channel": sd.channel,
            }
            for idx, cid in enumerate(new_ids):
                p_name = f"new_cid_{idx}"
                insert_params[p_name] = cid
                values_clauses.append(f"(:{p_name})")

            values_list = ", ".join(values_clauses)
            await conn.execute(
                text(
                    f"INSERT INTO segment_member "
                    f"(segment_id, contact_id, tenant, channel, added_at) "
                    f"SELECT :seg_id, cid::uuid, :tenant, :channel, now() "
                    f"FROM (VALUES {values_list}) AS v(cid) "
                    f"WHERE NOT EXISTS ("
                    f"  SELECT 1 FROM segment_member sm2 "
                    f"  WHERE sm2.segment_id = :seg_id "
                    f"    AND sm2.contact_id = cid::uuid "
                    f"    AND sm2.removed_at IS NULL"
                    f")"
                ),
                insert_params,
            )
            total_added += len(new_ids)

        # Soft-delete removed members in a single batch UPDATE.
        if removed_ids:
            await conn.execute(
                text(
                    "UPDATE segment_member SET removed_at = now() "
                    "WHERE segment_id = :seg_id "
                    "AND contact_id = ANY(:removed_ids) "
                    "AND removed_at IS NULL"
                ),
                {"seg_id": seg_id, "removed_ids": list(removed_ids)},
            )
        total_removed += len(removed_ids)

        # Compute excluded_by_guard_count: diff between unguarded and guarded counts.
        if not sd.acknowledges_cold and config.cold_exclusion.enabled:
            unguarded_sql, unguarded_params = compile_segment(
                predicate,
                config,
                now=now,
                acknowledges_cold=True,
                include_unsendable=sd.include_unsendable,
            )
            unguarded_q_params: dict[str, Any] = dict(unguarded_params) | {"tenant": sd.tenant}
            guarded_q_params: dict[str, Any] = dict(params) | {"tenant": sd.tenant}

            if dirty_contact_ids is not None:
                unguarded_sql = f"({unguarded_sql}) AND cp.contact_id = ANY(:dirty_ids)"
                unguarded_q_params["dirty_ids"] = dirty_contact_ids
                guarded_count_sql = f"({guarded_where_sql}) AND cp.contact_id = ANY(:dirty_ids)"
                guarded_q_params["dirty_ids"] = dirty_contact_ids
            else:
                guarded_count_sql = guarded_where_sql

            unguarded_count_result = await conn.execute(
                text(
                    f"SELECT COUNT(*) FROM contact_profile cp "
                    f"WHERE cp.tenant = :tenant AND {unguarded_sql}"
                ),
                unguarded_q_params,
            )
            unguarded_count = unguarded_count_result.scalar() or 0

            guarded_count_result = await conn.execute(
                text(
                    f"SELECT COUNT(*) FROM contact_profile cp "
                    f"WHERE cp.tenant = :tenant AND {guarded_count_sql}"
                ),
                guarded_q_params,
            )
            guarded_count = guarded_count_result.scalar() or 0

            excluded_by_guard_count = max(0, unguarded_count - guarded_count)
        else:
            excluded_by_guard_count = 0

        await conn.execute(
            text(
                "UPDATE segment_definition SET excluded_by_guard_count = :count, "
                "updated_at = now() WHERE segment_id = :seg_id"
            ),
            {"count": excluded_by_guard_count, "seg_id": seg_id},
        )

        log.info(
            "segment_materialize.segment_done",
            key=sd.key,
            added=len(new_ids),
            removed=len(removed_ids),
            excluded_by_guard_count=excluded_by_guard_count,
        )

    segments_processed = len(segment_defs)
    return {
        "segments_processed": segments_processed,
        "total_added": total_added,
        "total_removed": total_removed,
    }


# ---------------------------------------------------------------------------
# run_incremental
# ---------------------------------------------------------------------------


async def run_incremental() -> dict[str, Any]:
    """Recompute window aggregates and tier/score for dirty profile rows.

    Dirty = profile_version > module-level _watermark.
    Skips immediately if the lock is already held (previous run still in flight).
    Returns a summary dict.
    """
    global _watermark

    if _lock.locked():
        log.info("profile_refresh.incremental.skipped", reason="already_running")
        return {"skipped": True}

    async with _lock:
        t0 = time.monotonic()
        now = datetime.now(timezone.utc)
        watermark_at_start = _watermark

        async with async_session_factory() as session:
            async with session.begin():
                conn = session

                # 0. Deferred rule-1: reclassify pending clicks past confirm window.
                rule1_resolved = await _resolve_pending_clicks(conn)
                log.info(
                    "profile_refresh.incremental.rule1_resolved",
                    count=rule1_resolved,
                )

                # 1. Identify dirty rows.
                dirty_sql = text(
                    "SELECT tenant, contact_id, channel, profile_version "
                    "FROM contact_profile "
                    "WHERE profile_version > :watermark "
                    "ORDER BY profile_version "
                    "LIMIT :limit"
                )
                dirty_result = await conn.execute(
                    dirty_sql,
                    {"watermark": watermark_at_start, "limit": _INCREMENTAL_LIMIT},
                )
                dirty_rows = dirty_result.fetchall()

                if not dirty_rows:
                    log.info(
                        "profile_refresh.incremental.completed",
                        dirty_count=0,
                        elapsed_ms=0,
                    )
                    return {"dirty_count": 0, "elapsed_ms": 0}

                dirty_count = len(dirty_rows)
                max_version = max(r.profile_version for r in dirty_rows)

                # 2. Set-based window aggregate UPDATE.
                update_sql = text(
                    _WINDOW_UPDATE_SQL.format(dirty_cte=_DIRTY_CTE_INCREMENTAL)
                )
                await conn.execute(
                    update_sql,
                    {"watermark": watermark_at_start, "limit": _INCREMENTAL_LIMIT},
                )

                # 3. Fetch updated fields for tier/score computation.
                profile_rows = await _fetch_profiles_for_tier(
                    conn,
                    _DIRTY_CTE_INCREMENTAL,
                    {"watermark": watermark_at_start, "limit": _INCREMENTAL_LIMIT},
                )

                # 4. Compute tier/score via pure domain functions (Python pass).
                config = get_engagement_config()
                tier_updates = _compute_tier_and_score(profile_rows, now)

                # 5. Apply tier/score updates.
                await _apply_tier_score_updates(
                    conn, tier_updates, config.tier_config_version
                )

                # 6. Materialize segment membership for dirty contacts.
                seg_summary = await _materialize_segments(
                    conn,
                    dirty_contact_ids=[str(r.contact_id) for r in dirty_rows],
                )
                log.info(
                    "profile_refresh.incremental.segments_materialized",
                    **seg_summary,
                )

        # 7. Advance watermark after successful commit.
        _watermark = max_version

        elapsed_ms = round((time.monotonic() - t0) * 1000)
        log.info(
            "profile_refresh.incremental.completed",
            dirty_count=dirty_count,
            elapsed_ms=elapsed_ms,
            watermark=_watermark,
        )
        return {
            "dirty_count": dirty_count,
            "elapsed_ms": elapsed_ms,
            "watermark": _watermark,
            **seg_summary,
        }


# ---------------------------------------------------------------------------
# Provider daily rollup materialization
# ---------------------------------------------------------------------------


async def _materialize_provider_daily_rollup(conn: Any, tenant: str) -> int:
    """Compute and upsert provider_daily_rollup rows for the previous day.

    Sources contact_activity_daily (L2) joined to contact_profile to resolve
    reputation_group. Runs inside the nightly transaction after segment
    materialization. source is hard-coded to 1 (ses_events).

    Returns the number of rows upserted.
    """
    result = await conn.execute(
        text(
            """
            WITH prev_day AS (
                SELECT
                    cad.day,
                    cp.reputation_group,
                    cad.channel,
                    SUM(cad.sends)       AS sends,
                    SUM(cad.delivers)    AS delivers,
                    SUM(cad.bounces)     AS bounces_hard,
                    0                    AS bounces_soft,
                    SUM(cad.complaints)  AS complaints,
                    -- complaint_rate: denominator is delivers per spec §11
                    SUM(cad.complaints)::numeric
                        / NULLIF(SUM(cad.delivers), 0)  AS complaint_rate,
                    -- bounce_rate: single-day
                    SUM(cad.bounces)::numeric
                        / NULLIF(SUM(cad.delivers), 0)  AS bounce_rate
                FROM contact_activity_daily cad
                JOIN contact_profile cp
                  ON  cp.contact_id = cad.contact_id
                  AND cp.channel    = cad.channel
                  AND cp.tenant     = cad.tenant
                WHERE cad.day    = CURRENT_DATE - 1
                  AND cad.tenant = :tenant
                GROUP BY cad.day, cp.reputation_group, cad.channel
            ),
            window_7d AS (
                SELECT
                    cp.reputation_group,
                    cad.channel,
                    SUM(cad.complaints)::numeric
                        / NULLIF(SUM(cad.delivers), 0)  AS complaint_rate_7d
                FROM contact_activity_daily cad
                JOIN contact_profile cp
                  ON  cp.contact_id = cad.contact_id
                  AND cp.channel    = cad.channel
                  AND cp.tenant     = cad.tenant
                WHERE cad.day BETWEEN CURRENT_DATE - 7 AND CURRENT_DATE - 1
                  AND cad.tenant = :tenant
                GROUP BY cp.reputation_group, cad.channel
            ),
            window_30d AS (
                SELECT
                    cp.reputation_group,
                    cad.channel,
                    SUM(cad.complaints)::numeric
                        / NULLIF(SUM(cad.delivers), 0)  AS complaint_rate_30d
                FROM contact_activity_daily cad
                JOIN contact_profile cp
                  ON  cp.contact_id = cad.contact_id
                  AND cp.channel    = cad.channel
                  AND cp.tenant     = cad.tenant
                WHERE cad.day BETWEEN CURRENT_DATE - 30 AND CURRENT_DATE - 1
                  AND cad.tenant = :tenant
                GROUP BY cp.reputation_group, cad.channel
            )
            INSERT INTO provider_daily_rollup (
                day, tenant, reputation_group, channel, source,
                sends, delivers, bounces_hard, bounces_soft, complaints,
                complaint_rate, complaint_rate_7d, complaint_rate_30d, bounce_rate
            )
            SELECT
                pd.day,
                :tenant,
                pd.reputation_group,
                pd.channel,
                1                       AS source,
                pd.sends,
                pd.delivers,
                pd.bounces_hard,
                pd.bounces_soft,
                pd.complaints,
                pd.complaint_rate,
                w7.complaint_rate_7d,
                w30.complaint_rate_30d,
                pd.bounce_rate
            FROM prev_day pd
            LEFT JOIN window_7d  w7
              ON  w7.reputation_group = pd.reputation_group
              AND w7.channel          = pd.channel
            LEFT JOIN window_30d w30
              ON  w30.reputation_group = pd.reputation_group
              AND w30.channel          = pd.channel
            ON CONFLICT (tenant, day, reputation_group, channel, source)
            DO UPDATE SET
                sends              = EXCLUDED.sends,
                delivers           = EXCLUDED.delivers,
                bounces_hard       = EXCLUDED.bounces_hard,
                bounces_soft       = EXCLUDED.bounces_soft,
                complaints         = EXCLUDED.complaints,
                complaint_rate     = EXCLUDED.complaint_rate,
                complaint_rate_7d  = EXCLUDED.complaint_rate_7d,
                complaint_rate_30d = EXCLUDED.complaint_rate_30d,
                bounce_rate        = EXCLUDED.bounce_rate
            """
        ),
        {"tenant": tenant},
    )
    return result.rowcount


# ---------------------------------------------------------------------------
# run_nightly
# ---------------------------------------------------------------------------


def _check_guardrail(
    old_counts: dict[int, int],
    new_counts: dict[int, int],
    max_pct: float,
) -> bool:
    """Return True if any tier's population shift exceeds max_pct.

    Tiers with old_count == 0 are skipped (no division-by-zero).
    """
    all_tiers = set(old_counts) | set(new_counts)
    for tier in all_tiers:
        old_n = old_counts.get(tier, 0)
        new_n = new_counts.get(tier, 0)
        if old_n == 0:
            continue
        shift = abs(new_n - old_n) / old_n
        if shift > max_pct:
            return True
    return False


async def run_nightly() -> dict[str, Any]:
    """Full profile recompute from L2 with blast-radius guardrail (D4).

    Steps:
    1. Capture current tier distribution (old_counts) per tenant.
    2. Recompute all window aggregates from contact_activity_daily.
    3. Compute new engagement_tier for every profile in Python.
    4. Check guardrail: abort on any tier population shift > max_tier_shift_pct.
    5. Apply tier/score updates.
    6. Write segment_tier_snapshot rows per tenant per tier.
    """
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    config = get_engagement_config()
    max_shift = config.guardrail.max_tier_shift_pct

    # Load/upsert any new or changed YAML segment definitions before the main
    # transaction so that predicate changes are visible during materialization.
    # load_segments manages its own transaction (it commits internally).
    async with async_session_factory() as loader_session:
        load_result = await load_segments(loader_session)
        log.info("profile_refresh.nightly.segments_loaded", **load_result)

    async with async_session_factory() as session:
        async with session.begin():
            conn = session

            # ----------------------------------------------------------------
            # 1. Capture current tier distribution before any changes.
            # ----------------------------------------------------------------
            old_dist_result = await conn.execute(
                text(
                    "SELECT tenant, engagement_tier, COUNT(*) AS cnt "
                    "FROM contact_profile "
                    "WHERE engagement_tier IS NOT NULL "
                    "GROUP BY tenant, engagement_tier"
                )
            )
            # old_counts[tenant][tier] = count
            old_counts: dict[str, dict[int, int]] = {}
            for row in old_dist_result:
                old_counts.setdefault(row.tenant, {})[row.engagement_tier] = row.cnt

            # ----------------------------------------------------------------
            # 2. Recompute window aggregates for ALL profiles.
            # ----------------------------------------------------------------
            nightly_update_sql = text(
                _WINDOW_UPDATE_SQL.format(dirty_cte=_DIRTY_CTE_NIGHTLY)
            )
            await conn.execute(nightly_update_sql)

            # ----------------------------------------------------------------
            # 3. Fetch updated profile fields and compute new tiers/scores.
            # ----------------------------------------------------------------
            profile_rows = await _fetch_profiles_for_tier(
                conn,
                _DIRTY_CTE_NIGHTLY,
                {},
            )
            tier_updates = _compute_tier_and_score(profile_rows, now)

            # ----------------------------------------------------------------
            # 4. Blast-radius guardrail: compute new distribution in-memory
            #    and abort before commit if any tier shifts too far.
            # ----------------------------------------------------------------
            # new_counts[tenant][tier] = count
            new_counts: dict[str, dict[int, int]] = {}
            for upd in tier_updates:
                tenant = upd["p_tenant"]
                tier = upd["p_tier"]
                new_counts.setdefault(tenant, {})
                new_counts[tenant][tier] = new_counts[tenant].get(tier, 0) + 1

            breach_details: list[dict[str, Any]] = []
            all_tenants = set(old_counts) | set(new_counts)
            for tenant in all_tenants:
                old_t = old_counts.get(tenant, {})
                new_t = new_counts.get(tenant, {})
                if _check_guardrail(old_t, new_t, max_shift):
                    all_tiers = set(old_t) | set(new_t)
                    for tier in all_tiers:
                        old_n = old_t.get(tier, 0)
                        new_n = new_t.get(tier, 0)
                        if old_n == 0:
                            continue
                        shift = abs(new_n - old_n) / old_n
                        if shift > max_shift:
                            breach_details.append(
                                {
                                    "tenant": tenant,
                                    "tier": tier,
                                    "old_count": old_n,
                                    "new_count": new_n,
                                    "shift_pct": round(shift * 100, 2),
                                }
                            )

            if breach_details:
                # Transaction rolls back on exit (no explicit commit).
                log.error(
                    "profile_refresh.nightly.guardrail_breach",
                    breaches=breach_details,
                    max_tier_shift_pct=max_shift,
                )
                raise RuntimeError(
                    f"Blast-radius guardrail: {len(breach_details)} tier(s) exceeded "
                    f"max_tier_shift_pct={max_shift}. Details: {breach_details}"
                )

            # ----------------------------------------------------------------
            # 5. Apply tier/score updates.
            # ----------------------------------------------------------------
            await _apply_tier_score_updates(
                conn, tier_updates, config.tier_config_version
            )

            # ----------------------------------------------------------------
            # 6.5 Materialize segment membership for all contacts (full pass).
            # ----------------------------------------------------------------
            seg_summary = await _materialize_segments(conn)
            log.info(
                "profile_refresh.nightly.segments_materialized",
                **seg_summary,
            )

            # ----------------------------------------------------------------
            # 6.6 Materialize provider_daily_rollup for the previous day.
            # ----------------------------------------------------------------
            for tenant in new_counts.keys():
                rollup_rows = await _materialize_provider_daily_rollup(conn, tenant)
                log.info(
                    "profile_refresh.nightly.provider_rollup_materialized",
                    tenant=tenant,
                    rows=rollup_rows,
                )

            # ----------------------------------------------------------------
            # 6. Write segment_tier_snapshot rows per tenant per tier.
            # ----------------------------------------------------------------
            if new_counts:
                snapshot_sql = text(
                    "INSERT INTO segment_tier_snapshot "
                    "(id, tenant, run_at, tier, contact_count) "
                    "VALUES (gen_random_uuid(), :tenant, now(), :tier, :contact_count)"
                )
                for tenant, tier_map in new_counts.items():
                    for tier, cnt in tier_map.items():
                        await conn.execute(
                            snapshot_sql,
                            {"tenant": tenant, "tier": tier, "contact_count": cnt},
                        )

    elapsed_ms = round((time.monotonic() - t0) * 1000)
    total_profiles = len(tier_updates)
    log.info(
        "profile_refresh.nightly.completed",
        total_profiles=total_profiles,
        elapsed_ms=elapsed_ms,
        tenants=list(new_counts.keys()),
    )
    return {
        "total_profiles": total_profiles,
        "elapsed_ms": elapsed_ms,
        **seg_summary,
    }
