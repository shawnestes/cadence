"""Tests for GET /api/stats/isp-reputation (SPEC_ADDENDUM §2.4).

Per repo test conventions all rows use emails ending in `@cadencetests.io`
and campaign/template names starting with `test-`, so the autouse
`_clean_test_rows` fixture in tests/conftest.py purges them. Those emails
all classify as the "Other" ISP bucket (see tests/domain/test_isp.py for
exhaustive Gmail/Yahoo/Outlook/Apple classification coverage) — this test
focuses on the endpoint's DB+CloudWatch merge behavior, not the classifier
itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.adapters.esp import base as esp_base
from app.api import stats as stats_mod
from app.core.db import async_session_factory
from app.models.campaign import Campaign
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.template import Template


class _FakeAdapter:
    """Stand-in ESP adapter returning canned CloudWatch-derived data."""

    def __init__(self, snapshot: esp_base.ReputationSnapshot) -> None:
        self._snapshot = snapshot

    async def get_reputation(self) -> esp_base.ReputationSnapshot:
        return self._snapshot


class _FailingAdapter:
    async def get_reputation(self) -> esp_base.ReputationSnapshot:
        raise RuntimeError("cloudwatch unreachable")


async def _seed_campaign_template(db_session):
    c = Campaign(tenant="otto", name=f"test-campaign-{uuid.uuid4()}", vertical="auto")
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    t = Template(
        tenant="otto",
        campaign_id=c.id,
        cohort_key=f"test-cohort-{uuid.uuid4()}",
        variant_label="A",
        subject="Hi",
        body_html="<p>Hi</p>",
        body_text="Hi",
        status="approved",
    )
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return c, t


async def _seed_recipient(db_session, email: str) -> Recipient:
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
        attributes={},
    )
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)
    return r


async def _seed_send(db_session, recipient, campaign, template, status: str) -> Send:
    now = datetime.now(timezone.utc)
    s = Send(
        tenant="otto",
        campaign_id=campaign.id,
        template_id=template.id,
        recipient_id=recipient.id,
        cohort_key=template.cohort_key,
        variant_label=template.variant_label,
        esp="noop",
        esp_message_id=f"noop-{uuid.uuid4()}",
        rendered_subject="Hi",
        status=status,
        queued_at=now,
        sent_at=now,
        delivered_at=now if status == "delivered" else None,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.mark.asyncio
async def test_isp_reputation_requires_auth(client: AsyncClient):
    resp = await client.get("/api/stats/isp-reputation", params={"tenant": "otto"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_isp_reputation_combines_db_and_cloudwatch(
    client: AsyncClient, auth_headers: dict, db_session, monkeypatch
):
    campaign, template = await _seed_campaign_template(db_session)

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    recipient = await _seed_recipient(db_session, email)

    await _seed_send(db_session, recipient, campaign, template, "delivered")
    await _seed_send(db_session, recipient, campaign, template, "bounced")

    snapshot = esp_base.ReputationSnapshot(
        account_sending_enabled=True,
        bounce_rate_24h=0.01,
        complaint_rate_24h=0.0001,
        managed_sending_allowed=True,
        managed_dedicated_ip_count=4.0,
        isp_metrics=[
            esp_base.IspReputationMetric(
                isp="GMAIL", reputation=0.97, effective_daily_rate=50000.0
            ),
            esp_base.IspReputationMetric(
                isp="OTHER", reputation=0.5, effective_daily_rate=100.0
            ),
        ],
    )
    monkeypatch.setattr(stats_mod, "get_esp_adapter", lambda: _FakeAdapter(snapshot))

    resp = await client.get(
        "/api/stats/isp-reputation", params={"tenant": "otto"}, headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["sending_allowed"] is True
    assert data["dedicated_ip_count"] == 4

    by_isp = {row["isp"]: row for row in data["isps"]}
    assert set(by_isp) == {"Gmail", "Yahoo", "Outlook", "Apple", "Other"}

    # DB-derived counts land in "Other" (cadencetests.io isn't a real ISP
    # domain) and merge with that bucket's CloudWatch metric.
    other = by_isp["Other"]
    assert other["sent"] == 2
    assert other["delivered"] == 1
    assert other["delivery_rate"] == pytest.approx(0.5)
    assert other["reputation"] == pytest.approx(0.5)
    assert other["effective_daily_rate"] == pytest.approx(100.0)

    # Gmail has CloudWatch data but zero of our own sends this window.
    gmail = by_isp["Gmail"]
    assert gmail["sent"] == 0
    assert gmail["delivered"] == 0
    assert gmail["delivery_rate"] is None
    assert gmail["reputation"] == pytest.approx(0.97)
    assert gmail["effective_daily_rate"] == pytest.approx(50000.0)

    # Yahoo/Outlook/Apple have neither DB nor CloudWatch data.
    for isp in ("Yahoo", "Outlook", "Apple"):
        row = by_isp[isp]
        assert row["sent"] == 0
        assert row["delivered"] == 0
        assert row["delivery_rate"] is None
        assert row["reputation"] is None
        assert row["effective_daily_rate"] is None


@pytest.mark.asyncio
async def test_isp_reputation_degrades_gracefully_without_cloudwatch(
    client: AsyncClient, auth_headers: dict, monkeypatch
):
    monkeypatch.setattr(stats_mod, "get_esp_adapter", lambda: _FailingAdapter())

    resp = await client.get(
        "/api/stats/isp-reputation", params={"tenant": "otto"}, headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["sending_allowed"] is None
    assert data["dedicated_ip_count"] is None
    for row in data["isps"]:
        assert row["reputation"] is None
        assert row["effective_daily_rate"] is None
