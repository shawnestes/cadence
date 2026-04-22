"""Tests for the send worker (run_once)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.db import async_session_factory
from app.models.campaign import Campaign
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.suppression import Suppression
from app.models.template import Template
from app.workers import send_worker


async def _read_send(send_id):
    async with async_session_factory() as s:
        return (await s.execute(select(Send).where(Send.id == send_id))).scalar_one_or_none()


async def _seed_recipient(db_session, email: str, attrs: dict | None = None):
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
        attributes=attrs or {"first_name": "Sarah"},
    )
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)
    return r


async def _seed_campaign_template(
    db_session,
    subject: str = "Hi {{first_name}}",
    body_html: str = "<p>Hi {{first_name}}</p>",
    body_text: str = "Hi {{first_name}}",
):
    c = Campaign(
        tenant="otto",
        name=f"test-campaign-{uuid.uuid4()}",
        vertical="auto",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    t = Template(
        tenant="otto",
        campaign_id=c.id,
        cohort_key=f"test-cohort-{uuid.uuid4()}",
        variant_label="A",
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        status="approved",
    )
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return c, t


async def _seed_queued_send(db_session, campaign, template, recipient):
    now = datetime.now(timezone.utc)
    s = Send(
        tenant="otto",
        campaign_id=campaign.id,
        template_id=template.id,
        recipient_id=recipient.id,
        cohort_key=template.cohort_key,
        variant_label=template.variant_label,
        esp="noop",
        rendered_subject=template.subject,
        status="queued",
        queued_at=now,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


async def test_run_once_sends_queued_row(db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    c, t = await _seed_campaign_template(db_session)
    s = await _seed_queued_send(db_session, c, t, r)

    await send_worker.run_once()

    refreshed = await _read_send(s.id)
    assert refreshed is not None
    assert refreshed.status == "sent"
    assert refreshed.sent_at is not None
    assert refreshed.esp_message_id is not None
    assert refreshed.esp_message_id.startswith("noop-")


async def test_run_once_suppressed_skip(db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    c, t = await _seed_campaign_template(db_session)
    s = await _seed_queued_send(db_session, c, t, r)

    db_session.add(
        Suppression(
            tenant="otto",
            email=email,
            reason="unsubscribe",
            source="one_click",
        )
    )
    await db_session.commit()

    await send_worker.run_once()

    refreshed = await _read_send(s.id)
    assert refreshed.status == "failed"
    assert refreshed.failure_reason is not None
    assert "suppressed" in refreshed.failure_reason


async def test_run_once_missing_merge_tag(db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email, attrs={})
    c, t = await _seed_campaign_template(
        db_session,
        subject="Hi",
        body_html="<p>Hello {{nonexistent_var}}</p>",
        body_text="Hello {{nonexistent_var}}",
    )
    s = await _seed_queued_send(db_session, c, t, r)

    await send_worker.run_once()

    refreshed = await _read_send(s.id)
    assert refreshed.status == "failed"
    assert refreshed.failure_reason is not None
    assert "merge tag" in refreshed.failure_reason.lower()
