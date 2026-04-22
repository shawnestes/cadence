"""Tests for hard_bounces / soft_bounces fields on the topline endpoint."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.core.db import async_session_factory
from app.models.base import uuid7
from app.models.campaign import Campaign
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.template import Template


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_recipient(tenant: str, n: int) -> Recipient:
    now = datetime.now(timezone.utc)
    return Recipient(
        id=uuid7(),
        tenant=tenant,
        email=f"bounce-split-{n}@cadencetests.io",
        external_id=None,
        vertical="auto",
        first_seen_at=now,
        last_seen_at=now,
        consent_source="test",
        consent_at=now,
        status="active",
    )


def _make_campaign(tenant: str) -> Campaign:
    return Campaign(
        id=uuid7(),
        tenant=tenant,
        name="test-bounce-split-campaign",
        vertical="auto",
        status="active",
    )


def _make_template(campaign_id: uuid.UUID, tenant: str) -> Template:
    return Template(
        id=uuid7(),
        tenant=tenant,
        campaign_id=campaign_id,
        cohort_key="all",
        variant_label="A",
        subject="Test subject",
        body_html="<p>test</p>",
        body_text="test",
        status="approved",
    )


def _make_send(
    recipient_id: uuid.UUID,
    campaign_id: uuid.UUID,
    template_id: uuid.UUID,
    tenant: str,
    status: str,
    failure_reason: str | None = None,
) -> Send:
    return Send(
        id=uuid7(),
        tenant=tenant,
        campaign_id=campaign_id,
        template_id=template_id,
        recipient_id=recipient_id,
        cohort_key="all",
        variant_label="A",
        esp="ses",
        rendered_subject="Test subject",
        status=status,
        queued_at=datetime.now(timezone.utc),
        failure_reason=failure_reason,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_topline_bounce_split(client: AsyncClient, auth_headers: dict):
    """hard_bounces and soft_bounces should be counted correctly."""
    tenant = "otto"

    async with async_session_factory() as s:
        recipients = [_make_recipient(tenant, i) for i in range(5)]
        for r in recipients:
            s.add(r)
        await s.flush()

        campaign = _make_campaign(tenant)
        s.add(campaign)
        await s.flush()

        template = _make_template(campaign.id, tenant)
        s.add(template)
        await s.flush()

        # 2 hard bounces, 1 soft bounce, 1 delivered, 1 sent
        sends = [
            _make_send(recipients[0].id, campaign.id, template.id, tenant, "bounced", "hard_bounce"),
            _make_send(recipients[1].id, campaign.id, template.id, tenant, "bounced", "hard_bounce"),
            _make_send(recipients[2].id, campaign.id, template.id, tenant, "bounced", "soft_bounce"),
            _make_send(recipients[3].id, campaign.id, template.id, tenant, "delivered"),
            _make_send(recipients[4].id, campaign.id, template.id, tenant, "sent"),
        ]
        for snd in sends:
            s.add(snd)
        await s.commit()

    resp = await client.get(
        "/api/stats/topline",
        params={"tenant": tenant, "window": "24h"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    topline = data["topline"]

    assert topline["hard_bounces"] == 2
    assert topline["soft_bounces"] == 1
    assert topline["bounces"] == 3


@pytest.mark.asyncio
async def test_topline_bounce_split_zero_when_no_bounces(client: AsyncClient, auth_headers: dict):
    """hard_bounces and soft_bounces are both 0 when there are no bounced sends."""
    tenant = "otto"

    async with async_session_factory() as s:
        r = _make_recipient(tenant, 99)
        s.add(r)
        await s.flush()

        campaign = _make_campaign(tenant)
        s.add(campaign)
        await s.flush()

        template = _make_template(campaign.id, tenant)
        s.add(template)
        await s.flush()

        snd = _make_send(r.id, campaign.id, template.id, tenant, "delivered")
        s.add(snd)
        await s.commit()

    resp = await client.get(
        "/api/stats/topline",
        params={"tenant": tenant, "window": "24h"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    topline = resp.json()["topline"]
    assert topline["hard_bounces"] == 0
    assert topline["soft_bounces"] == 0
