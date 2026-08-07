from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._enrollment import EnrollmentOutcome, enroll_recipient
from app.core.config import get_engagement_config
from app.core.db import get_db
from app.domain.segment import normalize
from app.domain.workflow_defs import WorkflowDefinition
from app.models.suppression import Suppression
from app.models.workflow import Workflow
from app.models.workflow_version import WorkflowVersion
from app.schemas.segment import WorkflowBackfillRequest, WorkflowBackfillResponse
from app.schemas.workflow import (
    WorkflowIn,
    WorkflowListOut,
    WorkflowOut,
    WorkflowUpdate,
)
from app.services.segment_query import compile_segment

router = APIRouter(tags=["workflows"])


def _validate_or_400(definition: WorkflowDefinition) -> None:
    errors = definition.validate_graph()
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"definition_errors": errors},
        )


@router.post("/workflows", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    payload: WorkflowIn,
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    _validate_or_400(payload.definition)
    row = Workflow(
        tenant=payload.tenant,
        name=payload.name,
        vertical=payload.vertical,
        status="draft",
        definition=payload.definition.model_dump(),
        entry_segment=payload.entry_segment,
        version=1,
        created_by=payload.created_by,
        reentry_cooldown=payload.reentry_cooldown,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    db.add(
        WorkflowVersion(
            workflow_id=row.id,
            version=row.version,
            definition=row.definition,
        )
    )
    await db.commit()
    return WorkflowOut.model_validate(row)


@router.get("/workflows", response_model=WorkflowListOut)
async def list_workflows(
    tenant: str = Query(default="otto"),
    status_filter: str | None = Query(default=None, alias="status"),
    vertical: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> WorkflowListOut:
    base = select(Workflow).where(Workflow.tenant == tenant)
    count_q = select(func.count()).select_from(Workflow).where(Workflow.tenant == tenant)
    if status_filter is not None:
        base = base.where(Workflow.status == status_filter)
        count_q = count_q.where(Workflow.status == status_filter)
    if vertical is not None:
        base = base.where(Workflow.vertical == vertical)
        count_q = count_q.where(Workflow.vertical == vertical)

    base = base.order_by(Workflow.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(base)).scalars().all()
    total = (await db.execute(count_q)).scalar_one()

    return WorkflowListOut(
        workflows=[WorkflowOut.model_validate(r) for r in rows],
        total=int(total),
    )


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: UUID,
    tenant: str = Query(default="otto"),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    row = (
        await db.execute(
            select(Workflow)
            .where(Workflow.id == workflow_id)
            .where(Workflow.tenant == tenant)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return WorkflowOut.model_validate(row)


async def _load(db: AsyncSession, workflow_id: UUID, tenant: str) -> Workflow:
    row = (
        await db.execute(
            select(Workflow)
            .where(Workflow.id == workflow_id)
            .where(Workflow.tenant == tenant)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return row


@router.patch("/workflows/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(
    workflow_id: UUID,
    payload: WorkflowUpdate,
    tenant: str = Query(default="otto"),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    row = await _load(db, workflow_id, tenant)

    if payload.name is not None:
        row.name = payload.name
    if payload.entry_segment is not None:
        row.entry_segment = payload.entry_segment
    if payload.reentry_cooldown is not None:
        row.reentry_cooldown = payload.reentry_cooldown
    definition_changed = False
    if payload.definition is not None:
        _validate_or_400(payload.definition)
        row.definition = payload.definition.model_dump()
        row.version = row.version + 1
        definition_changed = True
    if payload.status is not None:
        if payload.status == "active":
            wd = WorkflowDefinition.model_validate(row.definition)
            _validate_or_400(wd)
        row.status = payload.status

    await db.commit()
    await db.refresh(row)
    if definition_changed:
        db.add(
            WorkflowVersion(
                workflow_id=row.id,
                version=row.version,
                definition=row.definition,
            )
        )
        await db.commit()
    return WorkflowOut.model_validate(row)


@router.post("/workflows/{workflow_id}/activate", response_model=WorkflowOut)
async def activate_workflow(
    workflow_id: UUID,
    tenant: str = Query(default="otto"),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    row = await _load(db, workflow_id, tenant)
    wd = WorkflowDefinition.model_validate(row.definition)
    _validate_or_400(wd)
    row.status = "active"
    await db.commit()
    await db.refresh(row)
    return WorkflowOut.model_validate(row)


@router.post("/workflows/{workflow_id}/pause", response_model=WorkflowOut)
async def pause_workflow(
    workflow_id: UUID,
    tenant: str = Query(default="otto"),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    row = await _load(db, workflow_id, tenant)
    row.status = "paused"
    await db.commit()
    await db.refresh(row)
    return WorkflowOut.model_validate(row)


@router.post("/workflows/{workflow_id}/archive", response_model=WorkflowOut)
async def archive_workflow(
    workflow_id: UUID,
    tenant: str = Query(default="otto"),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    row = await _load(db, workflow_id, tenant)
    row.status = "archived"
    await db.commit()
    await db.refresh(row)
    return WorkflowOut.model_validate(row)


@router.post("/workflows/{workflow_id}/backfill", response_model=WorkflowBackfillResponse)
async def backfill_workflow(
    workflow_id: UUID,
    payload: WorkflowBackfillRequest,
    tenant: str = Query(default="otto"),
    db: AsyncSession = Depends(get_db),
) -> WorkflowBackfillResponse:
    """Enroll existing recipients matching ``payload.segment`` into a workflow.

    Unlike ingest-time auto-enroll, the segment here is operator-supplied
    and independent of the workflow's own ``entry_segment`` — this lets an
    operator target a broader or different segment for a one-off backfill.
    Candidates are capped at ``payload.limit`` (<=10000, enforced by the
    schema) directly in the SQL query (ORDER BY id LIMIT), never pulling
    more rows than that into Python.
    """
    wf = await _load(db, workflow_id, tenant)

    entry_node_id = (wf.definition or {}).get("entry")
    if not entry_node_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workflow has no entry node defined; cannot backfill",
        )

    now = datetime.now(timezone.utc)
    try:
        normalize(payload.segment)
        where_sql, params = compile_segment(
            payload.segment, get_engagement_config(), now=now
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    params["tenant"] = tenant
    params["limit"] = payload.limit

    # The predicate targets contact_profile (cp); join back to recipients to
    # resolve the id/email the rest of this handler operates on.
    candidates = (
        await db.execute(
            text(
                "SELECT r.id AS id, r.email AS email "
                "FROM contact_profile cp "
                "JOIN recipients r ON r.id = cp.contact_id AND r.tenant = cp.tenant "
                f"WHERE cp.tenant = :tenant AND {where_sql} "
                "ORDER BY r.id LIMIT :limit"
            ),
            params,
        )
    ).all()

    suppressed_emails: set[str] = set()
    if candidates:
        rows = (
            await db.execute(
                select(Suppression.email)
                .where(Suppression.tenant == tenant)
                .where(Suppression.email.in_([r.email for r in candidates]))
            )
        ).scalars().all()
        suppressed_emails = {e.lower() for e in rows}

    enrolled = 0
    skipped_active = 0
    skipped_cooldown = 0
    skipped_suppressed = 0

    for recipient in candidates:
        if recipient.email.lower() in suppressed_emails:
            skipped_suppressed += 1
            continue

        result = await enroll_recipient(db, wf, recipient.id, tenant, now)
        if result.outcome == EnrollmentOutcome.ENROLLED:
            enrolled += 1
        elif result.outcome == EnrollmentOutcome.SKIPPED_ACTIVE:
            skipped_active += 1
        elif result.outcome == EnrollmentOutcome.SKIPPED_COOLDOWN:
            skipped_cooldown += 1
        # SKIPPED_NO_ENTRY_NODE can't happen here — checked up front above.

    return WorkflowBackfillResponse(
        enrolled=enrolled,
        skipped_active=skipped_active,
        skipped_cooldown=skipped_cooldown,
        skipped_suppressed=skipped_suppressed,
    )
