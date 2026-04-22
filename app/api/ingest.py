from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.domain.segment import matches as segment_matches
from app.models.journey import Journey
from app.models.recipient import Recipient
from app.models.workflow import Workflow
from app.schemas.recipient import RecipientIngestIn, RecipientIngestOut

router = APIRouter(tags=["ingest"])

log = structlog.get_logger(__name__)


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

    for wf in active_workflows:
        if not segment_matches(wf.entry_segment, attrs, vertical):
            continue
        try:
            definition = wf.definition or {}
            entry_node_id = definition.get("entry")
            if not entry_node_id:
                continue
            journey = Journey(
                tenant=tenant,
                workflow_id=wf.id,
                workflow_version=wf.version,
                recipient_id=recipient_id,
                current_node_id=entry_node_id,
                next_action_at=now,
                status="active",
                entered_at=now,
            )
            db.add(journey)
            await db.commit()
        except IntegrityError:
            await db.rollback()
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
