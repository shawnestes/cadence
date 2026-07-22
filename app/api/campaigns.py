from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.esp.factory import get_esp_adapter
from app.core.db import get_db
from app.domain.variant_picker import pick_variant
from app.models.campaign import Campaign
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.template import Template
from app.schemas.campaign import (
    CampaignIn,
    CampaignListOut,
    CampaignOut,
    CampaignSendRequest,
    CampaignSendResult,
    CampaignUpdate,
)

router = APIRouter(tags=["campaigns"])


@router.post("/campaigns", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignIn,
    db: AsyncSession = Depends(get_db),
) -> CampaignOut:
    row = Campaign(
        tenant=payload.tenant,
        workflow_id=payload.workflow_id,
        node_id=payload.node_id,
        name=payload.name,
        vertical=payload.vertical,
        cohort_filter=payload.cohort_filter,
        ai_brief=payload.ai_brief,
        mab_config=payload.mab_config,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return CampaignOut.model_validate(row)


@router.get("/campaigns", response_model=CampaignListOut)
async def list_campaigns(
    tenant: str = Query(default="otto"),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> CampaignListOut:
    base = select(Campaign).where(Campaign.tenant == tenant)
    count_q = select(func.count()).select_from(Campaign).where(Campaign.tenant == tenant)
    if status_filter is not None:
        base = base.where(Campaign.status == status_filter)
        count_q = count_q.where(Campaign.status == status_filter)

    base = base.order_by(Campaign.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(base)).scalars().all()
    total = (await db.execute(count_q)).scalar_one()

    return CampaignListOut(
        campaigns=[CampaignOut.model_validate(r) for r in rows],
        total=int(total),
    )


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> CampaignOut:
    row = (
        await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return CampaignOut.model_validate(row)


@router.patch("/campaigns/{campaign_id}", response_model=CampaignOut)
async def update_campaign(
    campaign_id: UUID,
    payload: CampaignUpdate,
    db: AsyncSession = Depends(get_db),
) -> CampaignOut:
    row = (
        await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            setattr(row, k, v)
    await db.commit()
    await db.refresh(row)
    return CampaignOut.model_validate(row)


@router.post("/campaigns/{campaign_id}/generate")
async def trigger_generation(
    campaign_id: UUID,
    variants_per_cohort: int = 5,
    db: AsyncSession = Depends(get_db),
) -> dict:
    c = (
        await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    ).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="campaign not found")
    from app.workers.ai_generator import generate_for_campaign
    result = await generate_for_campaign(campaign_id, variants_per_cohort=variants_per_cohort)
    return {
        "cohorts_processed": result.cohorts_processed,
        "templates_created": result.templates_created,
        "variants_total": result.variants_total,
        "errors": result.errors,
    }


@router.post("/campaigns/{campaign_id}/send", response_model=CampaignSendResult)
async def send_campaign(
    campaign_id: UUID,
    payload: CampaignSendRequest,
    db: AsyncSession = Depends(get_db),
) -> CampaignSendResult:
    campaign = (
        await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    ).scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    MAX_INPUTS = 10_000
    BATCH_SIZE = 500

    skipped_not_found = 0
    recipients: list[Recipient] = []

    if payload.recipient_ids:
        if len(payload.recipient_ids) > MAX_INPUTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"recipient_ids exceeds {MAX_INPUTS}",
            )
        rows = (
            await db.execute(
                select(Recipient)
                .where(Recipient.tenant == payload.tenant)
                .where(Recipient.id.in_(payload.recipient_ids))
            )
        ).scalars().all()
        found_ids = {r.id for r in rows}
        skipped_not_found = len([rid for rid in payload.recipient_ids if rid not in found_ids])
        recipients = list(rows)
    elif payload.email_list:
        if len(payload.email_list) > MAX_INPUTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"email_list exceeds {MAX_INPUTS}",
            )
        # Emails are stored lowercased at ingest; citext handles case but we
        # still normalize the input so membership checks line up.
        normalized_emails = [e.lower() for e in payload.email_list]
        rows = (
            await db.execute(
                select(Recipient)
                .where(Recipient.tenant == payload.tenant)
                .where(Recipient.email.in_(normalized_emails))
            )
        ).scalars().all()
        found_emails = {r.email.lower() for r in rows}
        skipped_not_found = len(
            [e for e in normalized_emails if e not in found_emails]
        )
        recipients = list(rows)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="recipient_ids or email_list required",
        )

    esp_name = get_esp_adapter().name
    now = datetime.now(timezone.utc)

    queued = 0
    skipped_no_template = 0

    # Preload the approved template set for this campaign once instead of
    # hitting the DB inside the per-recipient loop. pick_variant still runs
    # per-recipient for MAB Thompson sampling, but the in-memory variant
    # set means each call does at most one index lookup for stats.
    cohort_keys = {r.vertical for r in recipients} | {None}
    template_cache: dict[tuple[str | None], object] = {}
    for ck in cohort_keys:
        tmpl = await pick_variant(
            db, tenant=campaign.tenant, campaign_id=campaign.id, cohort_key=ck
        )
        template_cache[(ck,)] = tmpl

    for i, r in enumerate(recipients, start=1):
        template = template_cache.get((r.vertical,)) or template_cache.get((None,))
        if template is None:
            # Last-chance per-recipient resolution (MAB-driven) in case the
            # preloaded cache missed because of a cohort not seen above.
            template = await pick_variant(
                db, tenant=campaign.tenant, campaign_id=campaign.id, cohort_key=r.vertical
            )
            if template is None:
                template = await pick_variant(
                    db, tenant=campaign.tenant, campaign_id=campaign.id, cohort_key=None
                )
        if template is None:
            skipped_no_template += 1
            continue
        db.add(
            Send(
                tenant=campaign.tenant,
                campaign_id=campaign.id,
                template_id=template.id,
                recipient_id=r.id,
                cohort_key=template.cohort_key,
                variant_label=template.variant_label,
                esp=esp_name,
                rendered_subject=template.subject,
                status="queued",
                queued_at=now,
            )
        )
        queued += 1
        if i % BATCH_SIZE == 0:
            await db.commit()
    await db.commit()

    return CampaignSendResult(
        queued=queued,
        skipped_no_template=skipped_no_template,
        skipped_not_found=skipped_not_found,
    )
