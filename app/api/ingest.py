from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import desc, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.domain.duration import InvalidDurationError, parse_iso8601_duration
from app.domain.segment import matches as segment_matches
from app.models.journey import Journey
from app.models.recipient import Recipient
from app.models.workflow import Workflow
from app.schemas.recipient import RecipientIngestIn, RecipientIngestOut

router = APIRouter(tags=["ingest"])

log = structlog.get_logger(__name__)

# Statuses that represent an in-flight journey. Must match the partial
# unique index on journeys (uq_journeys_active_workflow_recipient).
_ACTIVE_STATUSES = ("active", "scheduled", "waiting")


async def _reentry_allowed(
    db: AsyncSession, wf: Workflow, recipient_id: object, now: datetime
) -> bool:
    """Decide whether ``recipient_id`` may (re-)enter workflow ``wf``.

    Looks at the most recent journey for this (workflow, recipient) pair:

    - No prior journey at all -> allowed (first-time entry).
    - Prior journey is still in-flight (active/scheduled/waiting) -> not
      allowed; the caller's ON CONFLICT DO NOTHING guard also covers this
      for concurrent inserts, this is just an early skip.
    - Prior journey is terminal (completed/exited/errored) -> governed by
      ``wf.reentry_cooldown`` (SPEC §6.5): NULL means never re-enter;
      otherwise re-entry is allowed once the cooldown has elapsed since
      the terminal journey's completion timestamp.
    """
    latest = (
        await db.execute(
            select(Journey)
            .where(Journey.workflow_id == wf.id)
            .where(Journey.recipient_id == recipient_id)
            .order_by(desc(Journey.entered_at))
            .limit(1)
        )
    ).scalar_one_or_none()

    if latest is None:
        return True
    if latest.status in _ACTIVE_STATUSES:
        return False

    # Terminal journey — apply the cooldown policy.
    if wf.reentry_cooldown is None:
        return False

    try:
        cooldown = parse_iso8601_duration(wf.reentry_cooldown)
    except InvalidDurationError as exc:
        # Should not happen — the schema validates on write — but don't
        # let a bad stored value silently permit unbounded re-entry.
        log.warning(
            "ingest.invalid_reentry_cooldown",
            workflow_id=str(wf.id),
            reentry_cooldown=wf.reentry_cooldown,
            error=str(exc),
        )
        return False

    terminal_at = latest.completed_at or latest.entered_at
    return (now - terminal_at) >= cooldown


@router.post("/ingest/recipients", response_model=RecipientIngestOut)
async def ingest_recipient(
    payload: RecipientIngestIn,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> RecipientIngestOut:
    now = datetime.now(timezone.utc)
    email = str(payload.email)

    insert_values: dict = {
        "tenant": payload.tenant,
        "email": email,
        "external_id": payload.external_id,
        "vertical": payload.vertical,
        "first_seen_at": now,
        "last_seen_at": now,
        "consent_source": payload.consent_source,
        "consent_at": payload.consent_at,
        "attributes": payload.attributes or {},
        "status": "active",
    }

    stmt = pg_insert(Recipient).values(**insert_values)

    update_set: dict = {
        "last_seen_at": now,
        "consent_source": stmt.excluded.consent_source,
        "consent_at": stmt.excluded.consent_at,
        "attributes": Recipient.__table__.c.attributes.op("||")(stmt.excluded.attributes),
        "updated_at": now,
    }
    if payload.external_id is not None:
        update_set["external_id"] = stmt.excluded.external_id

    stmt = stmt.on_conflict_do_update(
        index_elements=[Recipient.tenant, Recipient.email],
        set_=update_set,
    ).returning(
        Recipient.id,
        Recipient.tenant,
        Recipient.email,
        Recipient.status,
        literal_column("(xmax = 0)").label("created"),
    )

    result = await db.execute(stmt)
    row = result.one()
    await db.commit()

    recipient_id = row.id
    tenant = payload.tenant
    vertical = payload.vertical
    attrs = payload.attributes or {}

    try:
        active_workflows = (
            await db.execute(
                select(Workflow)
                .where(Workflow.tenant == tenant)
                .where(Workflow.status == "active")
            )
        ).scalars().all()
    except Exception as exc:
        await db.rollback()
        log.warning(
            "ingest.workflow_lookup_failed",
            recipient_id=str(recipient_id),
            error=str(exc),
        )
        active_workflows = []

    from app.models.base import uuid7 as _uuid7

    for wf in active_workflows:
        if not segment_matches(wf.entry_segment, attrs, vertical):
            continue
        try:
            definition = wf.definition or {}
            entry_node_id = definition.get("entry")
            if not entry_node_id:
                continue

            if not await _reentry_allowed(db, wf, recipient_id, now):
                continue

            # Use Core insert with on_conflict_do_nothing to avoid the
            # duplicate-key IntegrityError / async unwind / attribute
            # refresh dance that violates the greenlet boundary. The
            # partial unique index on (workflow_id, recipient_id) only
            # enforces on non-terminal statuses; _reentry_allowed above
            # applies the cooldown policy (SPEC §6.5). This guard remains
            # as a concurrency backstop against a race between the check
            # and the insert.
            jstmt = pg_insert(Journey).values(
                id=_uuid7(),
                tenant=tenant,
                workflow_id=wf.id,
                workflow_version=wf.version,
                recipient_id=recipient_id,
                current_node_id=entry_node_id,
                next_action_at=now,
                status="active",
                context={},
                entered_at=now,
            ).on_conflict_do_nothing()
            await db.execute(jstmt)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            log.warning(
                "ingest.journey_injection_failed",
                workflow_id=str(wf.id),
                recipient_id=str(recipient_id),
                error=str(exc),
            )

    response.status_code = (
        status.HTTP_201_CREATED if row.created else status.HTTP_200_OK
    )
    return RecipientIngestOut(
        id=str(row.id),
        tenant=row.tenant,
        email=row.email,
        status=row.status,
        created=bool(row.created),
    )
