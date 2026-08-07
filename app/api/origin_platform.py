"""Origin-platform ingest endpoint (M-SEG-4).

Receives click confirmations and conversion signals from the origin platform.

No API-key auth: this endpoint is called server-to-server from the origin
platform on a private network segment. Adding auth here would require the
origin platform to manage a rotating secret while providing no meaningful
additional security given the network boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.domain.event_codes import translate_ses_event_type
from app.models.base import uuid7

router = APIRouter(tags=["ingest"])

log = structlog.get_logger(__name__)

_CLICK_EVENT_TYPE: int = translate_ses_event_type("Click")
_CONVERSION_EVENT_TYPE: int = translate_ses_event_type("Conversion")

# classification constants
_CLASSIFICATION_HUMAN: int = 0
_CLASSIFICATION_SCANNER: int = 2

# source constants
_SOURCE_ORIGIN_PLATFORM: int = 2


class OriginPlatformEvent(BaseModel):
    event_type: Literal["click", "conversion"]
    click_id: str
    tenant: str
    occurred_at: datetime
    campaign_id: uuid.UUID | None = None
    meta: dict | None = None


@router.post("/origin-platform")
async def ingest_origin_platform(
    payload: OriginPlatformEvent,
    db: AsyncSession = Depends(get_db),
) -> dict:
    now = datetime.now(timezone.utc)
    settings = get_settings()

    if payload.event_type == "click":
        await _handle_click(db, payload, now)
    else:
        await _handle_conversion(db, payload, now, settings.attribution_window_days)

    await db.commit()
    return {"status": "ok"}


async def _handle_click(
    db: AsyncSession,
    payload: OriginPlatformEvent,
    now: datetime,
) -> None:
    """Reverse a pending scanner classification on an existing click."""
    # Upsert click_registry: mark as human and record reclassification time.
    await db.execute(
        text("""
            UPDATE click_registry
               SET classification   = :human,
                   reclassified_at  = :now
             WHERE click_id = :click_id
               AND tenant   = :tenant
        """),
        {
            "human": _CLASSIFICATION_HUMAN,
            "now": now,
            "click_id": payload.click_id,
            "tenant": payload.tenant,
        },
    )

    # Propagate classification to the matching contact_event row.
    await db.execute(
        text("""
            UPDATE contact_event
               SET classification = :human
             WHERE click_id   = :click_id
               AND event_type = :click_type
               AND tenant     = :tenant
        """),
        {
            "human": _CLASSIFICATION_HUMAN,
            "click_id": payload.click_id,
            "click_type": _CLICK_EVENT_TYPE,
            "tenant": payload.tenant,
        },
    )

    # SPEC §5 / §6: look up the SES click contact_event to get its event_id for dedup_of.
    ses_event_row = (
        await db.execute(
            text("""
                SELECT event_id, contact_id, occurred_at
                  FROM contact_event
                 WHERE click_id   = :click_id
                   AND event_type = :click_type
                   AND source     = 1
                   AND tenant     = :tenant
                 LIMIT 1
            """),
            {
                "click_id": payload.click_id,
                "click_type": _CLICK_EVENT_TYPE,
                "tenant": payload.tenant,
            },
        )
    ).mappings().one_or_none()

    if ses_event_row is not None:
        ses_event_id = ses_event_row["event_id"]
        contact_id = ses_event_row["contact_id"]
        occurred_at = ses_event_row["occurred_at"]

        # Insert origin-platform dedup contact_event row (source=2, dedup_of=ses_event_id).
        await db.execute(
            text("""
                INSERT INTO contact_event
                    (event_id, tenant, contact_id, channel, event_type,
                     occurred_at, source, click_id, classification, dedup_of)
                VALUES
                    (:event_id, :tenant, :contact_id, 1, :click_type,
                     :occurred_at, 2, :click_id, :classification, :dedup_of)
                ON CONFLICT DO NOTHING
            """),
            {
                "event_id": str(uuid7()),
                "tenant": payload.tenant,
                "contact_id": str(contact_id),
                "click_type": _CLICK_EVENT_TYPE,
                "occurred_at": occurred_at,
                "click_id": payload.click_id,
                "classification": _CLASSIFICATION_HUMAN,
                "dedup_of": str(ses_event_id),
            },
        )

        # Upsert contact_activity_daily: increment clicks counter.
        await db.execute(
            text("""
                INSERT INTO contact_activity_daily
                    (contact_id, tenant, channel, day, clicks)
                VALUES
                    (:contact_id, :tenant, 1, :day, 1)
                ON CONFLICT (tenant, contact_id, channel, day) DO UPDATE
                    SET clicks = contact_activity_daily.clicks + 1
            """),
            {
                "contact_id": str(contact_id),
                "tenant": payload.tenant,
                "day": occurred_at.date() if hasattr(occurred_at, "date") else now.date(),
            },
        )

        # Bump profile_version to mark contact dirty for the incremental pass.
        await db.execute(
            text("""
                UPDATE contact_profile
                   SET profile_version = profile_version + 1
                 WHERE contact_id = :contact_id
                   AND tenant     = :tenant
                   AND channel    = 1
            """),
            {
                "contact_id": str(contact_id),
                "tenant": payload.tenant,
            },
        )

    log.info(
        "origin_platform.click_confirmed",
        click_id=payload.click_id,
        tenant=payload.tenant,
    )


async def _handle_conversion(
    db: AsyncSession,
    payload: OriginPlatformEvent,
    now: datetime,
    attribution_window_days: int,
) -> None:
    """Record a conversion and optionally attribute it to a prior click."""
    # Resolve click_registry for attribution.
    reg_row = (
        await db.execute(
            text("""
                SELECT contact_id,
                       campaign_id,
                       clicked_at,
                       classification
                  FROM click_registry
                 WHERE click_id = :click_id
                   AND tenant   = :tenant
            """),
            {"click_id": payload.click_id, "tenant": payload.tenant},
        )
    ).mappings().one_or_none()

    attributed = False
    contact_id: int | None = None
    campaign_id: uuid.UUID | None = None
    original_clicked_at: datetime | None = None
    original_classification: int | None = None

    if reg_row is None:
        log.info(
            "origin_platform.conversion_unattributed",
            click_id=payload.click_id,
            tenant=payload.tenant,
            reason="click_id_not_found",
        )
    else:
        contact_id = reg_row["contact_id"]
        campaign_id = reg_row["campaign_id"]
        original_clicked_at = reg_row["clicked_at"]
        original_classification = reg_row["classification"]

        window_ok = (now - original_clicked_at).total_seconds() <= attribution_window_days * 86400
        if not window_ok:
            log.info(
                "origin_platform.conversion_unattributed",
                click_id=payload.click_id,
                tenant=payload.tenant,
                reason="outside_attribution_window",
                clicked_at=original_clicked_at.isoformat(),
            )
            # Outside the attribution window — clear campaign_id so the
            # contact_event is recorded unattributed (SPEC §6).
            campaign_id = None
        else:
            attributed = True

    # Insert the conversion contact_event when contact_id is known.
    # When click_id is unresolvable, contact_id is None and the schema's
    # NOT NULL constraint prevents the insert — log and skip.
    if contact_id is not None:
        await db.execute(
            text("""
                INSERT INTO contact_event
                    (event_id, contact_id, channel, event_type, occurred_at, campaign_id,
                     source, click_id, classification, meta, tenant)
                VALUES
                    (:event_id, :contact_id, :channel, :event_type, :occurred_at, :campaign_id,
                     :source, :click_id, :classification, CAST(:meta AS jsonb), :tenant)
            """),
            {
                "event_id": str(uuid7()),
                "contact_id": contact_id,
                "channel": 1,  # email
                "event_type": _CONVERSION_EVENT_TYPE,
                "occurred_at": payload.occurred_at,
                "campaign_id": str(campaign_id) if campaign_id else None,
                "source": _SOURCE_ORIGIN_PLATFORM,
                "click_id": payload.click_id,
                "classification": _CLASSIFICATION_HUMAN,
                "meta": None if payload.meta is None else __import__("json").dumps(payload.meta),
                "tenant": payload.tenant,
            },
        )

    if reg_row is not None:
        # Upsert click_registry: record conversion.
        await db.execute(
            text("""
                UPDATE click_registry
                   SET converted_at  = COALESCE(converted_at, :now),
                       convert_count = convert_count + 1
                 WHERE click_id = :click_id
                   AND tenant   = :tenant
            """),
            {"now": now, "click_id": payload.click_id, "tenant": payload.tenant},
        )

    if attributed and original_classification == _CLASSIFICATION_SCANNER:
        # Retroactive 5-step reclassification (SPEC §6).
        await _retroactive_reclassify(
            db,
            click_id=payload.click_id,
            tenant=payload.tenant,
            contact_id=contact_id,  # type: ignore[arg-type]
            original_clicked_at=original_clicked_at,  # type: ignore[arg-type]
            now=now,
        )

    log.info(
        "origin_platform.conversion_recorded",
        click_id=payload.click_id,
        tenant=payload.tenant,
        attributed=attributed,
    )


async def _retroactive_reclassify(
    db: AsyncSession,
    *,
    click_id: str,
    tenant: str,
    contact_id: int,
    original_clicked_at: datetime,
    now: datetime,
) -> None:
    """5-step retroactive reclassification for a scanner-flagged click (SPEC §6)."""
    # Step 1: reclassify click_registry entry.
    await db.execute(
        text("""
            UPDATE click_registry
               SET classification  = :human,
                   reclassified_at = :now
             WHERE click_id = :click_id
               AND tenant   = :tenant
        """),
        {
            "human": _CLASSIFICATION_HUMAN,
            "now": now,
            "click_id": click_id,
            "tenant": tenant,
        },
    )

    # Step 2: rewrite originating contact_event classification.
    await db.execute(
        text("""
            UPDATE contact_event
               SET classification = :human
             WHERE click_id   = :click_id
               AND event_type = :click_type
               AND tenant     = :tenant
        """),
        {
            "human": _CLASSIFICATION_HUMAN,
            "click_id": click_id,
            "click_type": _CLICK_EVENT_TYPE,
            "tenant": tenant,
        },
    )

    # Step 3: adjust daily counters on the original click date.
    click_day = original_clicked_at.date()
    await db.execute(
        text("""
            UPDATE contact_activity_daily
               SET clicks_bot = GREATEST(0, clicks_bot - 1),
                   clicks     = clicks + 1
             WHERE contact_id = :contact_id
               AND channel    = 1
               AND day        = :click_day
               AND tenant     = :tenant
        """),
        {
            "contact_id": contact_id,
            "click_day": click_day,
            "tenant": tenant,
        },
    )

    # Step 4: update contact_profile.last_click_at — take the max, never blind-assign.
    await db.execute(
        text("""
            UPDATE contact_profile
               SET last_click_at = GREATEST(last_click_at, :clicked_at)
             WHERE contact_id = :contact_id
               AND tenant     = :tenant
        """),
        {
            "clicked_at": original_clicked_at,
            "contact_id": contact_id,
            "tenant": tenant,
        },
    )

    # Step 5: bump profile_version to mark contact dirty for the incremental pass.
    await db.execute(
        text("""
            UPDATE contact_profile
               SET profile_version = profile_version + 1
             WHERE contact_id = :contact_id
               AND tenant     = :tenant
        """),
        {
            "contact_id": contact_id,
            "tenant": tenant,
        },
    )

    log.info(
        "origin_platform.retroactive_reclassify",
        click_id=click_id,
        tenant=tenant,
        contact_id=contact_id,
        original_clicked_at=original_clicked_at.isoformat(),
    )
