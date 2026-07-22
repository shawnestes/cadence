"""Tests for app.workers.engagement_scorer."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.db import async_session_factory
from app.domain.engagement import TIER_DORMANT, TIER_ENGAGED
from app.models.campaign import Campaign
from app.models.event import Event
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.suppression import Suppression
from app.models.template import Template
from app.models.unsub_outbox import UnsubOutbox
from app.workers import engagement_scorer


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

async def _seed_recipient(session, *, tier: str = "neutral") -> Recipient:
    now = datetime.now(timezone.utc)
    r = Recipient(
        tenant="otto",
        email=f"test-eng-{uuid.uuid4()}@cadencetests.io",
        external_id=None,
        vertical="auto",
        first_seen_at=now,
        last_seen_at=now,
        consent_source="test",
        consent_at=now,
        attributes={},
        status="active",
        engagement_tier=tier,
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return r


async def _seed_campaign_and_template(session) -> tuple[Campaign, Template]:
    c = Campaign(
        tenant="otto",
        name=f"test-eng-{uuid.uuid4()}",
        vertical="auto",
        status="active",
        mab_config={},
    )
    session.add(c)
    await session.flush()

    t = Template(
        tenant="otto",
        campaign_id=c.id,
        cohort_key="default",
        variant_label="A",
        subject="Test",
        body_html="<p>hi</p>",
        body_text="hi",
        status="approved",
    )
    session.add(t)
    await session.commit()
    await session.refresh(c)
    await session.refresh(t)
    return c, t


async def _seed_send(
    session,
    recipient: Recipient,
    campaign: Campaign,
    template: Template,
    sent_at: datetime,
) -> Send:
    s = Send(
        tenant="otto",
        journey_id=None,
        campaign_id=campaign.id,
        template_id=template.id,
        recipient_id=recipient.id,
        cohort_key="default",
        variant_label="A",
        esp="noop",
        rendered_subject="Test",
        status="delivered",
        queued_at=sent_at,
        sent_at=sent_at,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return s


async def _seed_open_event(session, recipient: Recipient, send: Send, occurred_at: datetime) -> None:
    session.add(
        Event(
            id=uuid.uuid4(),
            tenant="otto",
            send_id=send.id,
            recipient_id=recipient.id,
            campaign_id=send.campaign_id,
            variant_label=send.variant_label,
            cohort_key=send.cohort_key,
            event_type="open",
            occurred_at=occurred_at,
        )
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scorer_writes_tiers_for_engaged_and_dormant_recipients():
    """One recipient recently engaged (stays engaged, untouched beyond the
    tier write); one recipient with 10 unengaged sends transitions to
    dormant and gets suppressed + pushed to the outbox exactly once."""
    now = datetime.now(timezone.utc)

    async with async_session_factory() as session:
        campaign, template = await _seed_campaign_and_template(session)

        engaged = await _seed_recipient(session)
        engaged_send = await _seed_send(session, engaged, campaign, template, now - timedelta(hours=1))
        await _seed_open_event(session, engaged, engaged_send, now - timedelta(minutes=30))

        dormant = await _seed_recipient(session)
        for i in range(10):
            await _seed_send(session, dormant, campaign, template, now - timedelta(hours=10 - i))

    summary = await engagement_scorer.run_once()
    assert summary["scored"] >= 2
    assert summary["dormant_transitions"] >= 1

    async with async_session_factory() as session:
        eng_row = (
            await session.execute(select(Recipient).where(Recipient.id == engaged.id))
        ).scalar_one()
        dorm_row = (
            await session.execute(select(Recipient).where(Recipient.id == dormant.id))
        ).scalar_one()

        assert eng_row.engagement_tier == TIER_ENGAGED
        assert eng_row.tier_computed_at is not None
        assert eng_row.status == "active"

        assert dorm_row.engagement_tier == TIER_DORMANT
        assert dorm_row.status == "suppressed"

        suppressions = (
            await session.execute(
                select(Suppression).where(Suppression.email == dormant.email)
            )
        ).scalars().all()
        assert len(suppressions) == 1
        assert suppressions[0].reason == "engagement_decay"
        assert suppressions[0].source == "engagement_decay"

        outbox_rows = (
            await session.execute(
                select(UnsubOutbox).where(UnsubOutbox.email == dormant.email)
            )
        ).scalars().all()
        assert len(outbox_rows) == 1
        assert outbox_rows[0].reason == "engagement_decay"

    # Re-running must be idempotent: no duplicate suppression/outbox rows,
    # and the dormant transition is not double-counted (already dormant).
    summary2 = await engagement_scorer.run_once()
    assert summary2["dormant_transitions"] == 0

    async with async_session_factory() as session:
        suppressions = (
            await session.execute(
                select(Suppression).where(Suppression.email == dormant.email)
            )
        ).scalars().all()
        assert len(suppressions) == 1

        outbox_rows = (
            await session.execute(
                select(UnsubOutbox).where(UnsubOutbox.email == dormant.email)
            )
        ).scalars().all()
        assert len(outbox_rows) == 1


@pytest.mark.asyncio
async def test_scorer_leaves_recipient_with_no_activity_as_neutral():
    async with async_session_factory() as session:
        recipient = await _seed_recipient(session)

    await engagement_scorer.run_once()

    async with async_session_factory() as session:
        row = (
            await session.execute(select(Recipient).where(Recipient.id == recipient.id))
        ).scalar_one()
        assert row.engagement_tier == "neutral"
        assert row.tier_computed_at is not None
        assert row.status == "active"
