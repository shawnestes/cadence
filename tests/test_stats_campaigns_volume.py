"""Tests for GET /api/stats/campaigns-volume."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
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

def _make_recipient(tenant: str, tag: str) -> Recipient:
    now = datetime.now(timezone.utc)
    return Recipient(
        id=uuid7(),
        tenant=tenant,
        email=f"campvol-{tag}@cadencetests.io",
        external_id=None,
        vertical="auto",
        first_seen_at=now,
        last_seen_at=now,
        consent_source="test",
        consent_at=now,
        status="active",
    )


def _make_campaign(tenant: str, name: str) -> Campaign:
    return Campaign(
        id=uuid7(),
        tenant=tenant,
        name=name,
        vertical="auto",
        status="active",
    )


def _make_template(campaign_id: uuid.UUID, tenant: str, variant: str = "A") -> Template:
    return Template(
        id=uuid7(),
        tenant=tenant,
        campaign_id=campaign_id,
        cohort_key="all",
        variant_label=variant,
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
async def test_campaigns_volume_order_and_metrics(client: AsyncClient, auth_headers: dict):
    """Campaigns are ordered by sends desc; metrics are correct per campaign."""
    tenant = "otto"

    async with async_session_factory() as s:
        # Campaign A: 5 sends (3 delivered, 1 bounced, 1 complained)
        # Campaign B: 3 sends (2 delivered, 1 bounced)
        # Campaign C: 1 send  (1 delivered)
        campaigns = {
            "A": _make_campaign(tenant, "test-campvol-alpha"),
            "B": _make_campaign(tenant, "test-campvol-beta"),
            "C": _make_campaign(tenant, "test-campvol-gamma"),
        }
        for c in campaigns.values():
            s.add(c)
        await s.flush()

        templates = {k: _make_template(v.id, tenant) for k, v in campaigns.items()}
        for t in templates.values():
            s.add(t)
        await s.flush()

        recipients: list[Recipient] = []
        for i in range(9):
            r = _make_recipient(tenant, str(i))
            s.add(r)
            recipients.append(r)
        await s.flush()

        # Campaign A sends
        s.add(_make_send(recipients[0].id, campaigns["A"].id, templates["A"].id, tenant, "delivered"))
        s.add(_make_send(recipients[1].id, campaigns["A"].id, templates["A"].id, tenant, "delivered"))
        s.add(_make_send(recipients[2].id, campaigns["A"].id, templates["A"].id, tenant, "delivered"))
        s.add(_make_send(recipients[3].id, campaigns["A"].id, templates["A"].id, tenant, "bounced", "hard_bounce"))
        s.add(_make_send(recipients[4].id, campaigns["A"].id, templates["A"].id, tenant, "complained"))

        # Campaign B sends
        s.add(_make_send(recipients[5].id, campaigns["B"].id, templates["B"].id, tenant, "delivered"))
        s.add(_make_send(recipients[6].id, campaigns["B"].id, templates["B"].id, tenant, "delivered"))
        s.add(_make_send(recipients[7].id, campaigns["B"].id, templates["B"].id, tenant, "bounced", "soft_bounce"))

        # Campaign C sends
        s.add(_make_send(recipients[8].id, campaigns["C"].id, templates["C"].id, tenant, "delivered"))

        await s.commit()

    resp = await client.get(
        "/api/stats/campaigns-volume",
        params={"tenant": tenant, "window": "24h", "limit": 10},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["window"] == "24h"

    rows = data["campaigns"]
    # Should be ordered: A (5), B (3), C (1)
    names = [r["campaign_name"] for r in rows]
    assert names[:3] == [
        "test-campvol-alpha",
        "test-campvol-beta",
        "test-campvol-gamma",
    ]

    a = rows[0]
    assert a["sends"] == 5
    assert a["delivered"] == 3
    assert a["bounces"] == 1
    assert a["complaints"] == 1
    assert abs(a["delivery_rate"] - round(3 / 5, 6)) < 1e-5
    assert abs(a["bounce_rate"] - round(1 / 5, 6)) < 1e-5

    b = rows[1]
    assert b["sends"] == 3
    assert b["delivered"] == 2
    assert b["bounces"] == 1
    assert b["complaints"] == 0

    c = rows[2]
    assert c["sends"] == 1
    assert c["delivered"] == 1
    assert c["bounces"] == 0


@pytest.mark.asyncio
async def test_campaigns_volume_respects_limit(client: AsyncClient, auth_headers: dict):
    """limit parameter caps the number of campaigns returned."""
    tenant = "otto"

    async with async_session_factory() as s:
        camp_ids = []
        tmpl_ids = []
        for i in range(3):
            c = _make_campaign(tenant, f"test-campvol-limit-{i}")
            s.add(c)
            camp_ids.append(c)
        await s.flush()

        for c in camp_ids:
            t = _make_template(c.id, tenant)
            s.add(t)
            tmpl_ids.append(t)
        await s.flush()

        r = _make_recipient(tenant, "limit-base")
        s.add(r)
        await s.flush()

        for c, t in zip(camp_ids, tmpl_ids):
            s.add(_make_send(r.id, c.id, t.id, tenant, "delivered"))
        await s.commit()

    resp = await client.get(
        "/api/stats/campaigns-volume",
        params={"tenant": tenant, "window": "24h", "limit": 2},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()["campaigns"]) <= 2


@pytest.mark.asyncio
async def test_campaigns_volume_empty_window(client: AsyncClient, auth_headers: dict):
    """Returns empty list when no sends exist for the given window."""
    resp = await client.get(
        "/api/stats/campaigns-volume",
        params={"tenant": "nonexistent-tenant-xyz", "window": "24h"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["campaigns"] == []
