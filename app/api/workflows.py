from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.domain.workflow_defs import WorkflowDefinition
from app.models.workflow import Workflow
from app.schemas.workflow import (
    WorkflowIn,
    WorkflowListOut,
    WorkflowOut,
    WorkflowUpdate,
)

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
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
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
    if payload.definition is not None:
        _validate_or_400(payload.definition)
        row.definition = payload.definition.model_dump()
        row.version = row.version + 1
    if payload.status is not None:
        if payload.status == "active":
            wd = WorkflowDefinition.model_validate(row.definition)
            _validate_or_400(wd)
        row.status = payload.status

    await db.commit()
    await db.refresh(row)
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
