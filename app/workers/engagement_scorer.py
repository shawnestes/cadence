"""Nightly engagement scorer (SPEC_ADDENDUM_002 §2.1-2.2).

Classifies every active recipient into an engagement tier
(`app.domain.engagement.classify`) based on:

- `last_engagement_at`: the most recent open/click event.
- `consecutive_unengaged_sends`: sends dispatched after that last engagement
  (or all dispatched sends, if the recipient never engaged).

Both inputs are computed with one set-based SQL query per batch (never a
per-recipient query) using keyset pagination over `recipients.id` so the
worker scales to arbitrarily large recipient tables with bounded memory.

On a transition INTO `dormant`, the recipient is auto-suppressed
(`suppressions`, reason/source `engagement_decay`) and a distinguishable row
is pushed to `unsub_outbox` so OTTO can tell this apart from a real
unsubscribe. Both writes are idempotent: a recipient already suppressed is
skipped. Journeys are intentionally left untouched — the workflow tick's
global exit already drops suppressed recipients on its next pass.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from sqlalchemy import bindparam, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import async_session_factory
from app.domain.engagement import TIER_DORMANT, classify
from app.models.base import uuid7
from app.models.recipient import Recipient
from app.models.suppression import Suppression
from app.models.unsub_outbox import UnsubOutbox

log = structlog.get_logger(__name__)

_BATCH_SIZE = 1000

# Reason/source used for both the suppression row and the unsub_outbox row.
# Distinct from "unsubscribe", "hard_bounce", "complaint" so OTTO's sync can
# tell auto-suppression-on-decay apart from a real unsubscribe.
_DECAY_REASON = "engagement_decay"

_ENGAGEMENT_STATS_SQL = text(
    """
    WITH batch_ids AS (
        SELECT unnest(:ids) AS id
    ),
    last_eng AS (
        SELECT recipient_id, MAX(occurred_at) AS last_engagement_at
        FROM events
        WHERE event_type IN ('open', 'click')
          AND recipient_id IN (SELECT id FROM batch_ids)
        GROUP BY recipient_id
    ),
    unengaged AS (
        SELECT s.recipient_id, COUNT(*) AS consecutive_unengaged_sends
        FROM sends s
        LEFT JOIN last_eng le ON le.recipient_id = s.recipient_id
        WHERE s.recipient_id IN (SELECT id FROM batch_ids)
          AND s.sent_at IS NOT NULL
          AND (le.last_engagement_at IS NULL OR s.sent_at > le.last_engagement_at)
        GROUP BY s.recipient_id
    )
    SELECT
        b.id AS recipient_id,
        le.last_engagement_at AS last_engagement_at,
        COALESCE(u.consecutive_unengaged_sends, 0) AS consecutive_unengaged_sends
    FROM batch_ids b
    LEFT JOIN last_eng le ON le.recipient_id = b.id
    LEFT JOIN unengaged u ON u.recipient_id = b.id
    """
).bindparams(bindparam("ids", type_=ARRAY(PG_UUID(as_uuid=True))))


@dataclass(frozen=True)
class _EngagementStats:
    """Per-recipient engagement inputs for `classify()`."""

    last_engagement_at: datetime | None
    consecutive_unengaged_sends: int


async def _fetch_recipient_batch(
    db: AsyncSession, after_id: uuid.UUID | None
) -> list[Recipient]:
    """Fetch the next keyset-paginated batch of active recipients."""
    stmt = (
        select(Recipient)
        .where(Recipient.status == "active")
        .order_by(Recipient.id)
        .limit(_BATCH_SIZE)
    )
    if after_id is not None:
        stmt = stmt.where(Recipient.id > after_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _fetch_engagement_stats(
    db: AsyncSession, recipient_ids: list[uuid.UUID]
) -> dict[uuid.UUID, _EngagementStats]:
    """One set-based query for the whole batch: last engagement + consecutive
    unengaged send count per recipient. Never issued per-recipient."""
    result = await db.execute(_ENGAGEMENT_STATS_SQL, {"ids": recipient_ids})
    stats: dict[uuid.UUID, _EngagementStats] = {}
    for row in result.mappings():
        stats[row["recipient_id"]] = _EngagementStats(
            last_engagement_at=row["last_engagement_at"],
            consecutive_unengaged_sends=row["consecutive_unengaged_sends"],
        )
    return stats


async def _apply_dormant_transition(db: AsyncSession, recipient: Recipient, now: datetime) -> bool:
    """Suppress a recipient newly transitioned into `dormant`.

    Idempotent: if the recipient is already suppressed, the unique
    (tenant, email) constraint on `suppressions` raises `IntegrityError`
    inside a savepoint, which is swallowed — no outbox row is written either,
    since one was already pushed on the original transition.

    Returns True if a new suppression was created.
    """
    try:
        async with db.begin_nested():
            db.add(
                Suppression(
                    id=uuid7(),
                    tenant=recipient.tenant,
                    email=recipient.email,
                    reason=_DECAY_REASON,
                    source=_DECAY_REASON,
                )
            )
    except IntegrityError:
        log.debug(
            "engagement_scorer.already_suppressed",
            recipient_id=str(recipient.id),
        )
        return False

    db.add(
        UnsubOutbox(
            id=uuid7(),
            tenant=recipient.tenant,
            email=recipient.email,
            reason=_DECAY_REASON,
            occurred_at=now,
        )
    )
    recipient.status = "suppressed"
    return True


async def _score_batch(
    db: AsyncSession, batch: list[Recipient], now: datetime
) -> tuple[int, int]:
    """Classify + persist tiers for one batch. Returns (changed, dormant_transitions)."""
    settings = get_settings()
    recipient_ids = [r.id for r in batch]
    stats_by_id = await _fetch_engagement_stats(db, recipient_ids)

    update_rows: list[dict[str, object]] = []
    dormant_transitions = 0

    for recipient in batch:
        stats = stats_by_id.get(recipient.id, _EngagementStats(None, 0))
        new_tier = classify(
            last_engagement_at=stats.last_engagement_at,
            consecutive_unengaged_sends=stats.consecutive_unengaged_sends,
            now=now,
            engaged_window_days=settings.engagement_engaged_window_days,
            cold_after_unopened=settings.engagement_cold_after_unopened,
            dormant_after_unopened=settings.engagement_dormant_after_unopened,
        )
        old_tier = recipient.engagement_tier
        tier_changed = new_tier != old_tier
        if tier_changed or recipient.tier_computed_at is None:
            update_rows.append(
                {"id": recipient.id, "engagement_tier": new_tier, "tier_computed_at": now}
            )

        if tier_changed and new_tier == TIER_DORMANT:
            transitioned = await _apply_dormant_transition(db, recipient, now)
            if transitioned:
                dormant_transitions += 1

    if update_rows:
        await db.execute(update(Recipient), update_rows)

    await db.commit()
    return len(update_rows), dormant_transitions


async def run_once() -> dict:
    """Classify all active recipients and apply dormant auto-suppression.

    Returns a summary dict: {"scored": N, "changed": N, "dormant_transitions": N}.
    """
    now = datetime.now(timezone.utc)
    scored = 0
    changed = 0
    dormant_transitions = 0
    after_id: uuid.UUID | None = None

    async with async_session_factory() as db:
        while True:
            batch = await _fetch_recipient_batch(db, after_id)
            if not batch:
                break

            batch_changed, batch_dormant = await _score_batch(db, batch, now)
            scored += len(batch)
            changed += batch_changed
            dormant_transitions += batch_dormant
            after_id = batch[-1].id

            if len(batch) < _BATCH_SIZE:
                break

    log.info(
        "engagement_scorer.completed",
        scored=scored,
        changed=changed,
        dormant_transitions=dormant_transitions,
    )
    return {"scored": scored, "changed": changed, "dormant_transitions": dormant_transitions}
