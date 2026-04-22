"""Tests for app.workers.ai_generator.generate_for_campaign."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.db import async_session_factory
from app.models.campaign import Campaign
from app.models.recipient import Recipient
from app.models.template import Template
from app.workers.ai_generator import generate_for_campaign


async def _seed_campaign(db_session, *, ai_brief: str = "test brief") -> Campaign:
    c = Campaign(
        tenant="otto",
        name=f"test-{uuid.uuid4()}",
        vertical="auto",
        ai_brief=ai_brief,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _seed_recipient(db_session, email: str, attrs: dict) -> Recipient:
    now = datetime.now(timezone.utc)
    r = Recipient(
        tenant="otto",
        email=email,
        external_id="otto_test",
        vertical="auto",
        first_seen_at=now,
        last_seen_at=now,
        consent_source="otto_form",
        consent_at=now,
        attributes=attrs,
    )
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)
    return r


async def _fresh_read_templates(campaign_id):
    async with async_session_factory() as s:
        return (
            await s.execute(
                select(Template).where(Template.campaign_id == campaign_id)
            )
        ).scalars().all()


async def _fresh_read_campaign(campaign_id):
    async with async_session_factory() as s:
        return (
            await s.execute(select(Campaign).where(Campaign.id == campaign_id))
        ).scalar_one_or_none()


async def test_generate_for_campaign_with_mock_creates_templates(db_session):
    c = await _seed_campaign(db_session)
    await _seed_recipient(
        db_session,
        f"test-{uuid.uuid4()}@cadencetests.io",
        {"state": "TX", "age_band": "35-44"},
    )
    await _seed_recipient(
        db_session,
        f"test-{uuid.uuid4()}@cadencetests.io",
        {"state": "CA", "age_band": "25-34"},
    )

    campaign_id = c.id
    result = await generate_for_campaign(campaign_id, variants_per_cohort=3)

    assert result.errors == []
    assert result.cohorts_processed >= 1
    assert result.variants_total >= 3

    templates = await _fresh_read_templates(campaign_id)
    assert len(templates) >= 3

    refreshed = await _fresh_read_campaign(campaign_id)
    assert refreshed is not None
    assert refreshed.status == "ready"


async def test_generate_for_campaign_missing_returns_error():
    result = await generate_for_campaign(uuid.uuid4(), variants_per_cohort=3)
    assert result.cohorts_processed == 0
    assert result.variants_total == 0
    assert any("not found" in e for e in result.errors)


async def test_generate_for_campaign_no_recipients(db_session):
    c = await _seed_campaign(db_session)
    campaign_id = c.id

    result = await generate_for_campaign(campaign_id, variants_per_cohort=3)

    assert result.cohorts_processed == 0
    refreshed = await _fresh_read_campaign(campaign_id)
    assert refreshed is not None
    assert refreshed.status == "ready"
