"""Tests for POST /webhooks/ses using NoopESP parse path."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.db import async_session_factory
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.sending_identity import SendingIdentity
from app.models.suppression import Suppression
from app.models.unsub_outbox import UnsubOutbox
from tests._identity_helpers import create_identity


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
    db_session,
    recipient: Recipient,
    esp_message_id: str,
    sending_identity_id: uuid.UUID | None = None,
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
        sending_identity_id=sending_identity_id,
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


async def test_attribution_mismatch_logs_but_trusts_send_row(client, db_session, caplog):
    """SPEC_ADDENDUM_003 §6: attribution is telemetry only. When the webhook
    payload's configuration set maps to a different identity than the one
    recorded on the send row, we log a warning but still process the event
    against the send row's own identity — nothing about the send/event is
    changed because of the mismatch.
    """
    identity_a = await create_identity(db_session)
    identity_a.provider_config = {"configuration_set": f"test-config-a-{uuid.uuid4()}"}
    identity_b = await create_identity(db_session)
    identity_b.provider_config = {"configuration_set": f"test-config-b-{uuid.uuid4()}"}
    db_session.add_all([identity_a, identity_b])
    await db_session.commit()
    await db_session.refresh(identity_a)
    await db_session.refresh(identity_b)

    try:
        email = f"test-{uuid.uuid4()}@cadencetests.io"
        r = await _seed_recipient(db_session, email)
        msg_id = f"noop-{uuid.uuid4()}"
        s = await _seed_send(db_session, r, msg_id, sending_identity_id=identity_a.id)

        payload = _payload("delivery", email, msg_id)
        # NoopESP.parse_webhook stores the raw posted dict verbatim, so a flat
        # `configurationSetName` key exercises the fallback shape in
        # _extract_configuration_set — claiming identity_b's config set even
        # though the send row was dispatched under identity_a.
        payload["configurationSetName"] = identity_b.provider_config["configuration_set"]

        with caplog.at_level("WARNING"):
            resp = await client.post("/webhooks/ses", json=payload)
        assert resp.status_code == 200

        assert "webhook.identity_attribution_mismatch" in caplog.text
        assert str(identity_a.id) in caplog.text
        assert str(identity_b.id) in caplog.text

        # Send row is trusted: event still lands against send/identity_a,
        # nothing about sending_identity_id is rewritten from the payload.
        refreshed = await _fresh_read(select(Send).where(Send.id == s.id))
        assert refreshed.status == "delivered"
        assert refreshed.sending_identity_id == identity_a.id
    finally:
        # The webhook created an events row against this send, and Send FKs
        # to sending_identities; the autouse test-row purge in conftest runs
        # after this test returns, so unwind that chain ourselves first or
        # the identity delete below trips an FK constraint.
        async with async_session_factory() as cleanup:
            from sqlalchemy import text as sa_text

            await cleanup.execute(
                sa_text("DELETE FROM events WHERE send_id = :send_id"),
                {"send_id": s.id},
            )
            send_obj = await cleanup.get(Send, s.id)
            if send_obj is not None:
                await cleanup.delete(send_obj)
            for identity_id in (identity_a.id, identity_b.id):
                obj = await cleanup.get(SendingIdentity, identity_id)
                if obj is not None:
                    await cleanup.delete(obj)
            await cleanup.commit()
