"""Tests for app.workers.reputation_monitor."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.adapters.alerts.base import AlertEvent
from app.core.db import async_session_factory
from app.models.alert import Alert
from app.models.campaign import Campaign
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.template import Template
from app.workers import reputation_monitor


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

async def _seed_recipient(session) -> Recipient:
    now = datetime.now(timezone.utc)
    r = Recipient(
        tenant="otto",
        email=f"test-mon-{uuid.uuid4()}@cadencetests.io",
        external_id=None,
        vertical="auto",
        first_seen_at=now,
        last_seen_at=now,
        consent_source="test",
        consent_at=now,
        attributes={},
        status="active",
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return r


async def _seed_campaign_and_template(session) -> tuple[Campaign, Template]:
    c = Campaign(
        tenant="otto",
        name=f"test-mon-{uuid.uuid4()}",
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


async def _seed_sends_with_bounces(session, recipient: Recipient, campaign: Campaign, template: Template, n_sends: int, n_bounces: int):
    """Seed n_sends sends of which n_bounces are bounced."""
    now = datetime.now(timezone.utc)
    for i in range(n_sends):
        status = "bounced" if i < n_bounces else "delivered"
        failure_reason = "hard_bounce" if i < n_bounces else None
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
            status=status,
            queued_at=now,
            failure_reason=failure_reason,
        )
        session.add(s)
    await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_monitor_creates_pending_alert_on_breach():
    """First tick above critical threshold creates one pending row; no dispatch."""
    mock_dispatcher = AsyncMock()
    mock_dispatcher.notify = AsyncMock()

    async with async_session_factory() as session:
        r = await _seed_recipient(session)
        c, t = await _seed_campaign_and_template(session)
        # 10 sends, 6 bounced → 60% bounce rate (>> 5% critical)
        await _seed_sends_with_bounces(session, r, c, t, 10, 6)

    await reputation_monitor.run_once(dispatcher=mock_dispatcher)

    # Should have created one pending alert for bounce_rate, no dispatch yet
    async with async_session_factory() as session:
        stmt = select(Alert).where(
            Alert.tenant == "otto",
            Alert.metric == "bounce_rate",
        )
        alerts = (await session.execute(stmt)).scalars().all()

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.status == "pending"
    assert alert.severity == "critical"
    assert alert.value > 0.05
    mock_dispatcher.notify.assert_not_called()


@pytest.mark.asyncio
async def test_monitor_promotes_to_active_after_sustained():
    """Second tick with pending_since backdated beyond sustained_minutes promotes to active."""
    mock_dispatcher = AsyncMock()
    mock_dispatcher.notify = AsyncMock()

    async with async_session_factory() as session:
        r = await _seed_recipient(session)
        c, t = await _seed_campaign_and_template(session)
        await _seed_sends_with_bounces(session, r, c, t, 10, 6)

    # First tick → creates pending
    await reputation_monitor.run_once(dispatcher=mock_dispatcher)
    mock_dispatcher.notify.assert_not_called()

    # Backdate pending_since to exceed sustained_minutes (default 30)
    async with async_session_factory() as session:
        stmt = select(Alert).where(
            Alert.tenant == "otto",
            Alert.metric == "bounce_rate",
            Alert.status == "pending",
        )
        alert = (await session.execute(stmt)).scalar_one()
        alert.pending_since = datetime.now(timezone.utc) - timedelta(minutes=35)
        await session.commit()

    # Second tick → should promote to active and dispatch
    await reputation_monitor.run_once(dispatcher=mock_dispatcher)

    async with async_session_factory() as session:
        stmt = select(Alert).where(
            Alert.tenant == "otto",
            Alert.metric == "bounce_rate",
        )
        alert = (await session.execute(stmt)).scalar_one()

    assert alert.status == "active"
    assert alert.activated_at is not None

    mock_dispatcher.notify.assert_called_once()
    call_args = mock_dispatcher.notify.call_args[0][0]
    assert isinstance(call_args, AlertEvent)
    assert call_args.status_transition == "activated"
    assert call_args.metric == "bounce_rate"


@pytest.mark.asyncio
async def test_monitor_resolves_alert_when_below_threshold():
    """Third tick: after seeding no more bounces, existing active alert is resolved."""
    mock_dispatcher = AsyncMock()
    mock_dispatcher.notify = AsyncMock()

    async with async_session_factory() as session:
        r = await _seed_recipient(session)
        c, t = await _seed_campaign_and_template(session)
        await _seed_sends_with_bounces(session, r, c, t, 10, 6)

    # Create initial pending alert
    await reputation_monitor.run_once(dispatcher=mock_dispatcher)

    # Backdate and promote to active
    async with async_session_factory() as session:
        stmt = select(Alert).where(Alert.tenant == "otto", Alert.metric == "bounce_rate")
        alert = (await session.execute(stmt)).scalar_one()
        alert.pending_since = datetime.now(timezone.utc) - timedelta(minutes=35)
        await session.commit()

    await reputation_monitor.run_once(dispatcher=mock_dispatcher)
    mock_dispatcher.notify.reset_mock()

    # Now clear all bounced sends so rate drops below threshold
    async with async_session_factory() as session:
        from sqlalchemy import text
        await session.execute(
            text("DELETE FROM sends WHERE tenant = 'otto' AND recipient_id = :rid"),
            {"rid": r.id},
        )
        # Seed sends with zero bounces
        for i in range(10):
            s = Send(
                tenant="otto",
                journey_id=None,
                campaign_id=c.id,
                template_id=t.id,
                recipient_id=r.id,
                cohort_key="default",
                variant_label="A",
                esp="noop",
                rendered_subject="Test",
                status="delivered",
                queued_at=datetime.now(timezone.utc),
                failure_reason=None,
            )
            session.add(s)
        await session.commit()

    # Third tick → should resolve
    await reputation_monitor.run_once(dispatcher=mock_dispatcher)

    async with async_session_factory() as session:
        stmt = select(Alert).where(Alert.tenant == "otto", Alert.metric == "bounce_rate")
        alert = (await session.execute(stmt)).scalar_one()

    assert alert.status == "resolved"
    assert alert.resolved_at is not None

    mock_dispatcher.notify.assert_called_once()
    call_args = mock_dispatcher.notify.call_args[0][0]
    assert call_args.status_transition == "resolved"


@pytest.mark.asyncio
async def test_slack_dispatcher_no_op_when_no_webhook_url():
    """SlackAlertDispatcher is a no-op when webhook URL is unset."""
    from app.adapters.alerts.slack import SlackAlertDispatcher
    from unittest.mock import patch

    dispatcher = SlackAlertDispatcher()
    event = AlertEvent(
        alert_id=uuid.uuid4(),
        tenant="otto",
        metric="bounce_rate",
        severity="critical",
        value=0.06,
        threshold=0.05,
        status_transition="activated",
    )

    with patch("app.adapters.alerts.slack.get_settings") as mock_settings:
        mock_settings.return_value.slack_webhook_url = None
        # Should not raise; just logs and returns
        await dispatcher.notify(event)
