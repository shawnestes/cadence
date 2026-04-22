"""Tests for POST /webhooks/ses using NoopESP parse path."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.db import async_session_factory
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.suppression import Suppression
from app.models.unsub_outbox import UnsubOutbox


async def _fresh_read(stmt):
    async with async_session_factory() as s:
        return (await s.execute(stmt)).scalar_one_or_none()


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


async def _seed_campaign_template(db_session):
    from app.models.campaign import Campaign
    from app.models.template import Template

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
        subject="Hi",
        body_html="<p>Hi</p>",
        body_text="Hi",
        status="approved",
    )
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return c, t


async def _seed_send(
    db_session, recipient: Recipient, esp_message_id: str
) -> Send:
    c, t = await _seed_campaign_template(db_session)
    now = datetime.now(timezone.utc)
    s = Send(
        tenant="otto",
        campaign_id=c.id,
        template_id=t.id,
        recipient_id=recipient.id,
        cohort_key=t.cohort_key,
        variant_label=t.variant_label,
        esp="noop",
        esp_message_id=esp_message_id,
        rendered_subject="Hi",
        status="sent",
        queued_at=now,
        sent_at=now,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


def _payload(event_type: str, email: str, message_id: str, subtype=None) -> dict:
    p = {
        "event_type": event_type,
        "email": email,
        "message_id": message_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    if subtype:
        p["event_subtype"] = subtype
    return p


async def test_delivery_webhook_marks_send_delivered(client, db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    msg_id = f"noop-{uuid.uuid4()}"
    s = await _seed_send(db_session, r, msg_id)

    resp = await client.post(
        "/webhooks/ses",
        json=_payload("delivery", email, msg_id),
    )
    assert resp.status_code == 200

    refreshed = await _fresh_read(select(Send).where(Send.id == s.id))
    assert refreshed.status == "delivered"
    assert refreshed.delivered_at is not None


async def test_hard_bounce_creates_suppression(client, db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    msg_id = f"noop-{uuid.uuid4()}"
    s = await _seed_send(db_session, r, msg_id)

    resp = await client.post(
        "/webhooks/ses",
        json=_payload("bounce", email, msg_id, subtype="hard"),
    )
    assert resp.status_code == 200

    refreshed = await _fresh_read(select(Send).where(Send.id == s.id))
    assert refreshed.status == "bounced"

    sup = await _fresh_read(select(Suppression).where(Suppression.email == email))
    assert sup is not None
    assert sup.reason == "hard_bounce"

    outbox = await _fresh_read(select(UnsubOutbox).where(UnsubOutbox.email == email))
    assert outbox is not None

    refreshed_r = await _fresh_read(select(Recipient).where(Recipient.id == r.id))
    assert refreshed_r.status == "bounced_hard"


async def test_complaint_creates_suppression(client, db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    msg_id = f"noop-{uuid.uuid4()}"
    await _seed_send(db_session, r, msg_id)

    resp = await client.post(
        "/webhooks/ses",
        json=_payload("complaint", email, msg_id),
    )
    assert resp.status_code == 200

    sup = await _fresh_read(select(Suppression).where(Suppression.email == email))
    assert sup is not None
    assert sup.reason == "complaint"

    refreshed_r = await _fresh_read(select(Recipient).where(Recipient.id == r.id))
    assert refreshed_r.status == "complained"


async def test_subscription_confirmation_is_200(client):
    payload = {
        "Type": "SubscriptionConfirmation",
        "TopicArn": "arn:aws:sns:us-east-1:000:fake",
        "SubscribeURL": "http://127.0.0.1:1/does-not-exist",
    }
    resp = await client.post("/webhooks/ses", json=payload)
    assert resp.status_code == 200


async def test_malformed_body_returns_200(client):
    resp = await client.post(
        "/webhooks/ses",
        content=b"not json at all {{{",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
