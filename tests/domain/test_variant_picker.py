"""Tests for MAB-aware template picker."""

from __future__ import annotations

import uuid

from app.core.db import async_session_factory
from app.domain.variant_picker import pick_variant
from app.models.campaign import Campaign
from app.models.template import Template


async def _seed_campaign(db_session) -> Campaign:
    c = Campaign(
        tenant="otto",
        name=f"test-{uuid.uuid4()}",
        vertical="auto",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _seed_template(
    db_session,
    campaign: Campaign,
    *,
    cohort_key: str,
    variant_label: str,
    status: str = "approved",
) -> Template:
    t = Template(
        tenant=campaign.tenant,
        campaign_id=campaign.id,
        cohort_key=cohort_key,
        variant_label=variant_label,
        subject="Hi {{first_name}}",
        body_html="<p>Hi</p>",
        body_text="Hi",
        status=status,
    )
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


async def test_pick_returns_approved_template(db_session):
    c = await _seed_campaign(db_session)
    t = await _seed_template(
        db_session, c, cohort_key="auto:TX", variant_label="A"
    )

    async with async_session_factory() as s:
        picked = await pick_variant(
            s, tenant="otto", campaign_id=c.id, cohort_key="auto:TX"
        )
    assert picked is not None
    assert picked.id == t.id


async def test_pick_returns_none_when_no_approved(db_session):
    c = await _seed_campaign(db_session)
    await _seed_template(
        db_session, c, cohort_key="auto:TX", variant_label="A", status="generated"
    )
    await _seed_template(
        db_session, c, cohort_key="auto:TX", variant_label="B", status="rejected"
    )

    async with async_session_factory() as s:
        picked = await pick_variant(
            s, tenant="otto", campaign_id=c.id, cohort_key="auto:TX"
        )
    assert picked is None


async def test_pick_falls_back_to_cohort_none(db_session):
    c = await _seed_campaign(db_session)
    t = await _seed_template(
        db_session, c, cohort_key="auto:X", variant_label="A"
    )

    async with async_session_factory() as s:
        picked = await pick_variant(
            s, tenant="otto", campaign_id=c.id, cohort_key="auto:Y"
        )
    assert picked is not None
    assert picked.id == t.id


async def test_pick_prefers_matching_cohort(db_session):
    c = await _seed_campaign(db_session)
    t_x = await _seed_template(
        db_session, c, cohort_key="auto:X", variant_label="A"
    )
    await _seed_template(
        db_session, c, cohort_key="auto:Y", variant_label="B"
    )

    async with async_session_factory() as s:
        picked = await pick_variant(
            s, tenant="otto", campaign_id=c.id, cohort_key="auto:X"
        )
    assert picked is not None
    assert picked.id == t_x.id
    assert picked.cohort_key == "auto:X"
