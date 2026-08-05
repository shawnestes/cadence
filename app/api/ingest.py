from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._enrollment import EnrollmentOutcome, enroll_recipient
from app.core.db import get_db
from app.domain.segment import matches as segment_matches
from app.models.recipient import Recipient
from app.models.workflow import Workflow
from app.schemas.recipient import RecipientIngestIn, RecipientIngestOut
from app.schemas.records import RecordBatchIn, RecordBatchOut, RecordResult
from app.services.record_ingest import IngestCounters, flush_unknown_domains, ingest_one_row
from app.services.tenant_mapping import load_tenant_mapping

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
            enroll_result = await enroll_recipient(db, wf, recipient_id, tenant, now)
            if enroll_result.outcome not in (
                EnrollmentOutcome.ENROLLED,
                EnrollmentOutcome.SKIPPED_ACTIVE,
                EnrollmentOutcome.SKIPPED_COOLDOWN,
                EnrollmentOutcome.SKIPPED_NO_ENTRY_NODE,
            ):  # pragma: no cover - exhaustive by construction
                log.warning(
                    "ingest.unexpected_enrollment_outcome",
                    workflow_id=str(wf.id),
                    recipient_id=str(recipient_id),
                    outcome=enroll_result.outcome,
                )
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


@router.post("/ingest/records/batch", response_model=RecordBatchOut)
async def ingest_records_batch(
    payload: RecordBatchIn,
    db: AsyncSession = Depends(get_db),
) -> RecordBatchOut:
    """Bulk record ingest. Same service the CLI uses (spec §11)."""
    if payload.enroll:
        raise HTTPException(400, "enroll is not supported on the batch endpoint yet")

    mapping = load_tenant_mapping(payload.tenant)
    now = datetime.now(timezone.utc)
    counters = IngestCounters()
    results: list[RecordResult] = []
    unknown_domains: set[str] = set()

    for index, raw in enumerate(payload.records):
        counters.read += 1
        try:
            # A statement failure (e.g. a value the DB rejects) poisons the
            # session for anything after it. A SAVEPOINT scopes that damage
            # to this one row: on error, SQLAlchemy rolls back to it on
            # __aexit__, leaving `db` usable for the rows that follow within
            # the same transaction and the same final commit below.
            async with db.begin_nested():
                outcome, _ = await ingest_one_row(
                    raw,
                    db,
                    tenant=payload.tenant,
                    vertical=payload.vertical,
                    mapping=mapping,
                    now=now,
                )
        except Exception as exc:  # noqa: BLE001 - isolate one bad row from the rest of the batch
            counters.rejected += 1
            log.warning(
                "ingest.batch_row_failed",
                tenant=payload.tenant,
                index=index,
                error=str(exc),
            )
            results.append(RecordResult(index=index, status="error", errors=[str(exc)[:500]]))
            continue

        if outcome.unknown_domain:
            unknown_domains.add(outcome.unknown_domain)
        if outcome.status == "created":
            counters.loaded += 1
        elif outcome.status == "updated":
            counters.updated += 1
        elif outcome.status == "quarantined":
            counters.quarantined += 1
        else:
            counters.rejected += 1
        results.append(
            RecordResult(index=index, status=outcome.status, errors=list(outcome.errors))
        )

    await db.commit()
    await flush_unknown_domains(payload.tenant, unknown_domains)
    return RecordBatchOut(results=results, counters=counters.as_dict())
