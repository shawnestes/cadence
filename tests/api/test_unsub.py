"""Tests for GET/POST /u/{token}."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.db import async_session_factory
from app.core.unsub import UnsubPayload, get_unsub_signer
from app.models.recipient import Recipient
from app.models.suppression import Suppression
from app.models.unsub_outbox import UnsubOutbox


async def _fresh_all(stmt):
    async with async_session_factory() as s:
        return (await s.execute(stmt)).scalars().all()


async def _fresh_one(stmt):
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


def _token_for(recipient: Recipient) -> str:
    signer = get_unsub_signer()
    return signer.sign(
        UnsubPayload(
            recipient_id=str(recipient.id),
            send_id=None,
            tenant=recipient.tenant,
        )
    )


async def test_get_shows_form(client, db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    token = _token_for(r)

    resp = await client.get(f"/u/{token}")
    assert resp.status_code == 200
    assert "Unsubscribe" in resp.text


async def test_post_one_click_inserts_suppression(client, db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    token = _token_for(r)

    resp = await client.post(f"/u/{token}")
    assert resp.status_code == 200

    sup = await _fresh_one(select(Suppression).where(Suppression.email == email))
    assert sup is not None

    outbox = await _fresh_one(select(UnsubOutbox).where(UnsubOutbox.email == email))
    assert outbox is not None

    refreshed = await _fresh_one(select(Recipient).where(Recipient.id == r.id))
    assert refreshed.status == "suppressed"


async def test_post_idempotent(client, db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    token = _token_for(r)

    first = await client.post(f"/u/{token}")
    assert first.status_code == 200
    second = await client.post(f"/u/{token}")
    assert second.status_code == 200

    rows = await _fresh_all(select(Suppression).where(Suppression.email == email))
    assert len(rows) == 1


async def test_expired_token_returns_410(client, db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    # Corrupt the token (valid shape, bad signature).
    token = _token_for(r) + "xxx"

    resp = await client.post(f"/u/{token}")
    assert resp.status_code == 410


async def test_bad_token_returns_410(client):
    resp = await client.post("/u/invalid-token")
    assert resp.status_code == 410


async def test_post_unknown_recipient_shows_done_page_and_records_diagnostic(client):
    from app.models.unsub_diagnostic import UnsubDiagnostic

    signer = get_unsub_signer()
    ghost_id = str(uuid.uuid4())
    token = signer.sign(UnsubPayload(recipient_id=ghost_id, send_id=None, tenant="otto"))

    resp = await client.post(f"/u/{token}")

    assert resp.status_code == 200
    assert "unsubscribed" in resp.text.lower()
    diag = await _fresh_one(
        select(UnsubDiagnostic).where(UnsubDiagnostic.recipient_id == uuid.UUID(ghost_id))
    )
    assert diag is not None
    assert diag.reason == "unknown_recipient"
    assert diag.tenant == "otto"


async def test_get_unknown_recipient_shows_inactive_page_not_error_text(client):
    signer = get_unsub_signer()
    token = signer.sign(
        UnsubPayload(recipient_id=str(uuid.uuid4()), send_id=None, tenant="otto")
    )

    resp = await client.get(f"/u/{token}")

    assert resp.status_code == 410
    assert "not found" not in resp.text.lower()
    assert "isn't active" in resp.text
