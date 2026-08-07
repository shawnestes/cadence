"""Shared contact_profile/consent_proof seeding for tests/api/.

Segment predicates compile against ``contact_profile`` (see
``app/services/segment_query.py``), but ``POST /api/ingest/recipients`` only
writes a ``recipients`` row. Any API test that exercises a segment-based
endpoint (segment preview, campaign send-by-segment, workflow backfill) must
seed a matching ``contact_profile`` row (and its required ``consent_proof``
FK) itself. Mirrors the raw-SQL seeding pattern already used in
``tests/workers/test_segment_materialize.py``.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text

from app.core.db import async_session_factory

_TENANT = "otto"
_CHANNEL = 1  # email

TIER_ACTIVE = 1
MBP_GMAIL = 1


async def _seed_consent_proof(contact_id: str) -> str:
    proof_id = str(uuid.uuid4())
    async with async_session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO consent_proof
                    (consent_proof_id, tenant, contact_id, captured_at,
                     disclosure_text_hash, channels)
                VALUES
                    (:id, :tenant, :contact_id, now(), 'deadbeef', ARRAY[1]::smallint[])
                """
            ),
            {"id": proof_id, "tenant": _TENANT, "contact_id": contact_id},
        )
        await s.commit()
    return proof_id


async def seed_contact_profile(
    contact_id: str,
    *,
    email_domain: str | None = None,
    mailbox_provider: int = MBP_GMAIL,
    engagement_tier: int = TIER_ACTIVE,
    consent_state: int = 1,
    suppressed_at: datetime | None = None,
) -> None:
    """Insert (or upsert) a sendable contact_profile row for ``contact_id``.

    Defaults produce a row that is sendable (``consent_state=1``,
    ``suppressed_at=None``) and not cold-tiered, so it survives the guards
    ``compile_segment`` appends by default.
    """
    proof_id = await _seed_consent_proof(contact_id)
    async with async_session_factory() as s:
        await s.execute(
            text(
                """
                INSERT INTO contact_profile
                    (contact_id, tenant, channel, email_domain, mailbox_provider,
                     engagement_tier, consent_state, consent_proof_id,
                     suppressed_at, profile_version, tier_config_version, updated_at)
                VALUES
                    (:cid, :tenant, :channel, :email_domain, :mbp,
                     :tier, :consent_state, :proof_id,
                     :suppressed_at, 1, 'test-config-v1', now())
                ON CONFLICT (tenant, contact_id, channel) DO UPDATE SET
                    email_domain     = EXCLUDED.email_domain,
                    mailbox_provider = EXCLUDED.mailbox_provider,
                    engagement_tier  = EXCLUDED.engagement_tier,
                    consent_state    = EXCLUDED.consent_state,
                    suppressed_at    = EXCLUDED.suppressed_at,
                    updated_at       = now()
                """
            ),
            {
                "cid": contact_id,
                "tenant": _TENANT,
                "channel": _CHANNEL,
                "email_domain": email_domain,
                "mbp": mailbox_provider,
                "tier": engagement_tier,
                "consent_state": consent_state,
                "proof_id": proof_id,
                "suppressed_at": suppressed_at,
            },
        )
        await s.commit()
