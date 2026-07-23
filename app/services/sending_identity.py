"""Sending-identity resolution (SPEC_ADDENDUM_003).

Every send is dispatched as exactly one identity: the campaign's explicit
`sending_identity_id`, else the tenant's default identity.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import Campaign
from app.models.sending_identity import SendingIdentity


async def get_identity(
    db: AsyncSession, identity_id: uuid.UUID
) -> SendingIdentity | None:
    return (
        await db.execute(
            select(SendingIdentity).where(SendingIdentity.id == identity_id)
        )
    ).scalar_one_or_none()


async def tenant_default(db: AsyncSession, tenant: str) -> SendingIdentity | None:
    return (
        await db.execute(
            select(SendingIdentity)
            .where(SendingIdentity.tenant == tenant)
            .where(SendingIdentity.is_default)
        )
    ).scalar_one_or_none()


async def resolve_for_campaign(
    db: AsyncSession, campaign: Campaign
) -> SendingIdentity | None:
    """The identity this campaign sends as, or None if none configured."""
    if campaign.sending_identity_id is not None:
        identity = await get_identity(db, campaign.sending_identity_id)
        if identity is not None:
            return identity
    return await tenant_default(db, campaign.tenant)
