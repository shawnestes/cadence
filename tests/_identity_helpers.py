"""Shared sending-identity fixtures for SPEC_ADDENDUM_003 tests.

Not a test module itself (no `test_` prefix) -- imported by the ESP/send-path
test suites that need a `SendingIdentity` row to stamp sends against.
`sending_identities` isn't purged by the `_clean_test_rows` autouse fixture
in `tests/conftest.py` (owned by another agent), so these helpers are
get-or-create / uniquely-named rather than insert-unconditionally, to stay
safe across repeated test runs against the same database.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sending_identity import SendingIdentity


async def get_or_create_default_identity(
    db_session: AsyncSession, tenant: str = "otto"
) -> SendingIdentity:
    """The tenant's default identity, creating one (SES, active) if absent.

    Mirrors the migration's seed identity, since a fresh test DB migrates
    with no `campaigns`/`sends` rows yet and so seeds nothing.
    """
    existing = (
        await db_session.execute(
            select(SendingIdentity)
            .where(SendingIdentity.tenant == tenant)
            .where(SendingIdentity.is_default.is_(True))
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    identity = SendingIdentity(
        tenant=tenant,
        name="default",
        esp_provider="ses",
        from_address="test-default@cadencetests.io",
        sending_domain="cadencetests.io",
        provider_config={"region": "us-east-1", "account_key": "default"},
        status="active",
        is_default=True,
    )
    db_session.add(identity)
    await db_session.commit()
    await db_session.refresh(identity)
    return identity


async def create_identity(
    db_session: AsyncSession,
    tenant: str = "otto",
    *,
    status: str = "active",
    esp_provider: str = "ses",
    is_default: bool = False,
    **overrides,
) -> SendingIdentity:
    """A uniquely-named, non-default-by-default identity for one test."""
    identity = SendingIdentity(
        tenant=tenant,
        name=f"test-identity-{uuid.uuid4()}",
        esp_provider=esp_provider,
        from_address=f"test-{uuid.uuid4()}@cadencetests.io",
        sending_domain="cadencetests.io",
        provider_config={"region": "us-east-1"},
        status=status,
        is_default=is_default,
        **overrides,
    )
    db_session.add(identity)
    await db_session.commit()
    await db_session.refresh(identity)
    return identity
