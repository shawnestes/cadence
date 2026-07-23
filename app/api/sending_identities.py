"""CRUD for sending identities (SPEC_ADDENDUM_003 §8). Pause, don't delete."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.sending_identity import SendingIdentity
from app.schemas.sending_identity import (
    SendingIdentityIn,
    SendingIdentityListOut,
    SendingIdentityOut,
    SendingIdentityUpdate,
)

router = APIRouter(tags=["sending-identities"])


@router.get("/sending-identities", response_model=SendingIdentityListOut)
async def list_sending_identities(
    tenant: str = Query(default="otto"),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> SendingIdentityListOut:
    base = select(SendingIdentity).where(SendingIdentity.tenant == tenant)
    count_q = (
        select(func.count())
        .select_from(SendingIdentity)
        .where(SendingIdentity.tenant == tenant)
    )
    if status_filter is not None:
        base = base.where(SendingIdentity.status == status_filter)
        count_q = count_q.where(SendingIdentity.status == status_filter)

    base = base.order_by(SendingIdentity.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(base)).scalars().all()
    total = (await db.execute(count_q)).scalar_one()

    return SendingIdentityListOut(
        sending_identities=[SendingIdentityOut.model_validate(r) for r in rows],
        total=int(total),
    )


@router.post(
    "/sending-identities",
    response_model=SendingIdentityOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_sending_identity(
    payload: SendingIdentityIn,
    db: AsyncSession = Depends(get_db),
) -> SendingIdentityOut:
    row = SendingIdentity(
        tenant=payload.tenant,
        name=payload.name,
        esp_provider=payload.esp_provider,
        from_address=payload.from_address,
        sending_domain=payload.sending_domain,
        credentials_ref=payload.credentials_ref,
        provider_config=payload.provider_config,
        daily_quota=payload.daily_quota,
        status=payload.status,
        is_default=payload.is_default,
    )
    if payload.is_default:
        # Clear any existing default for this tenant in the same transaction
        # as the insert so the partial unique index (one default per tenant)
        # never sees a transient conflict.
        await db.execute(
            update(SendingIdentity)
            .where(SendingIdentity.tenant == payload.tenant)
            .where(SendingIdentity.is_default.is_(True))
            .values(is_default=False)
        )
    db.add(row)
    try:
        await db.commit()
        await db.refresh(row)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="sending identity with this tenant/name already exists",
        )
    return SendingIdentityOut.model_validate(row)


@router.get("/sending-identities/{identity_id}", response_model=SendingIdentityOut)
async def get_sending_identity(
    identity_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> SendingIdentityOut:
    row = (
        await db.execute(
            select(SendingIdentity).where(SendingIdentity.id == identity_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return SendingIdentityOut.model_validate(row)


@router.patch("/sending-identities/{identity_id}", response_model=SendingIdentityOut)
async def update_sending_identity(
    identity_id: UUID,
    payload: SendingIdentityUpdate,
    db: AsyncSession = Depends(get_db),
) -> SendingIdentityOut:
    row = (
        await db.execute(
            select(SendingIdentity).where(SendingIdentity.id == identity_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    data = payload.model_dump(exclude_unset=True)
    if data.get("is_default") is True:
        # Same atomic clear-then-set as create: flip the previous default off
        # before this row's own update commits, all in one transaction.
        await db.execute(
            update(SendingIdentity)
            .where(SendingIdentity.tenant == row.tenant)
            .where(SendingIdentity.id != row.id)
            .where(SendingIdentity.is_default.is_(True))
            .values(is_default=False)
        )
    for k, v in data.items():
        if v is not None:
            setattr(row, k, v)

    try:
        await db.commit()
        await db.refresh(row)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="sending identity with this tenant/name already exists",
        )
    return SendingIdentityOut.model_validate(row)
