from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.event import Event
from app.models.journey import Journey
from app.models.recipient import Recipient
from app.models.send import Send
from app.schemas.recipients import (
    EventSummary,
    JourneySummary,
    RecipientDetail,
    RecipientSearchOut,
    RecipientSummary,
    SendSummary,
)

router = APIRouter(tags=["recipients"])


@router.get("/recipients", response_model=RecipientSearchOut)
async def search_recipients(
    email: str | None = Query(default=None),
    tenant: str = Query(default="otto"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> RecipientSearchOut:
    base = select(Recipient).where(Recipient.tenant == tenant)
    count_q = select(func.count()).select_from(Recipient).where(Recipient.tenant == tenant)
    if email is not None and email.strip() != "":
        base = base.where(Recipient.email == email)
        count_q = count_q.where(Recipient.email == email)

    base = base.order_by(Recipient.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(base)).scalars().all()
    total = (await db.execute(count_q)).scalar_one()

    return RecipientSearchOut(
        recipients=[RecipientSummary.model_validate(r) for r in rows],
        total=int(total),
    )


@router.get("/recipients/{recipient_id}", response_model=RecipientDetail)
async def get_recipient(
    recipient_id: UUID,
    tenant: str = Query(default="otto"),
    db: AsyncSession = Depends(get_db),
) -> RecipientDetail:
    recipient = (
        await db.execute(
            select(Recipient)
            .where(Recipient.id == recipient_id)
            .where(Recipient.tenant == tenant)
        )
    ).scalar_one_or_none()
    if recipient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    journeys = (
        await db.execute(
            select(Journey)
            .where(Journey.recipient_id == recipient_id)
            .where(Journey.tenant == tenant)
            .order_by(Journey.entered_at.desc())
            .limit(20)
        )
    ).scalars().all()

    sends = (
        await db.execute(
            select(Send)
            .where(Send.recipient_id == recipient_id)
            .where(Send.tenant == tenant)
            .order_by(Send.queued_at.desc())
            .limit(50)
        )
    ).scalars().all()

    events = (
        await db.execute(
            select(Event)
            .where(Event.recipient_id == recipient_id)
            .where(Event.tenant == tenant)
            .order_by(Event.occurred_at.desc())
            .limit(100)
        )
    ).scalars().all()

    return RecipientDetail(
        recipient=RecipientSummary.model_validate(recipient),
        journeys=[JourneySummary.model_validate(j) for j in journeys],
        sends=[SendSummary.model_validate(s) for s in sends],
        events=[EventSummary.model_validate(e) for e in events],
    )
