from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.template import Template
from app.schemas.template import (
    TemplateIn,
    TemplateListOut,
    TemplateOut,
    TemplateUpdate,
)

router = APIRouter(tags=["templates"])


@router.post("/templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: TemplateIn,
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    row = Template(
        tenant=payload.tenant,
        campaign_id=payload.campaign_id,
        cohort_key=payload.cohort_key,
        variant_label=payload.variant_label,
        subject=payload.subject,
        preheader=payload.preheader,
        body_html=payload.body_html,
        body_text=payload.body_text,
        ai_meta=payload.ai_meta,
    )
    db.add(row)
    try:
        await db.commit()
        await db.refresh(row)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="template with this campaign_id/cohort_key/variant_label already exists",
        )
    return TemplateOut.model_validate(row)


@router.get("/templates", response_model=TemplateListOut)
async def list_templates(
    campaign_id: UUID = Query(...),
    status_filter: str | None = Query(default=None, alias="status"),
    tenant: str = Query(default="otto"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> TemplateListOut:
    base = (
        select(Template)
        .where(Template.tenant == tenant)
        .where(Template.campaign_id == campaign_id)
    )
    count_q = (
        select(func.count())
        .select_from(Template)
        .where(Template.tenant == tenant)
        .where(Template.campaign_id == campaign_id)
    )
    if status_filter is not None:
        base = base.where(Template.status == status_filter)
        count_q = count_q.where(Template.status == status_filter)

    base = base.order_by(Template.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(base)).scalars().all()
    total = (await db.execute(count_q)).scalar_one()

    return TemplateListOut(
        templates=[TemplateOut.model_validate(r) for r in rows],
        total=int(total),
    )


@router.get("/templates/{template_id}", response_model=TemplateOut)
async def get_template(
    template_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    row = (
        await db.execute(select(Template).where(Template.id == template_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return TemplateOut.model_validate(row)


@router.patch("/templates/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: UUID,
    payload: TemplateUpdate,
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    row = (
        await db.execute(select(Template).where(Template.id == template_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            setattr(row, k, v)
    await db.commit()
    await db.refresh(row)
    return TemplateOut.model_validate(row)
