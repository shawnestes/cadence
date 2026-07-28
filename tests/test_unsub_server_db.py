"""unsub_server.db against the cadence test database. Seeds rows with
cadence's ORM (test-side convenience only; the server itself is ORM-free)."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core.db import engine as cadence_engine
from app.models.recipient import Recipient
from app.models.suppression import Suppression
from app.models.unsub_outbox import UnsubOutbox
from unsub_server import db as udb


@pytest.fixture(autouse=True)
def _point_at_test_db(monkeypatch):
    url = cadence_engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("UNSUB_DATABASE_URL", url)
    udb.reset_engine()
    yield
    udb.reset_engine()


async def _seed_recipient(db_session, email: str) -> Recipient:
    now = datetime.now(timezone.utc)
    r = Recipient(
        tenant="otto",
        email=email,
        vertical="auto",
        first_seen_at=now,
        last_seen_at=now,
        consent_source="test",
        consent_at=now,
        attributes={},
    )
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)
    return r


async def test_fetch_recipient_found(db_session):
    r = await _seed_recipient(db_session, f"unsub-db-{uuid.uuid4()}@cadencetests.io")
    row = await udb.fetch_recipient("otto", str(r.id))
    assert row is not None
    assert row["email"] == r.email
    assert row["status"] == "active"


async def test_fetch_recipient_wrong_tenant_is_none(db_session):
    r = await _seed_recipient(db_session, f"unsub-db-{uuid.uuid4()}@cadencetests.io")
    assert await udb.fetch_recipient("not-otto", str(r.id)) is None


async def test_fetch_recipient_invalid_uuid_is_none(db_session):
    assert await udb.fetch_recipient("otto", "not-a-uuid") is None


async def test_record_unsub_writes_all_three(db_session):
    r = await _seed_recipient(db_session, f"unsub-db-{uuid.uuid4()}@cadencetests.io")

    await udb.record_unsub("otto", str(r.id), r.email)

    sup = (
        await db_session.execute(
            select(Suppression).where(Suppression.tenant == "otto", Suppression.email == r.email)
        )
    ).scalar_one()
    assert sup.reason == "unsubscribe"
    assert sup.source == "one_click"

    outbox = (
        await db_session.execute(
            select(UnsubOutbox).where(UnsubOutbox.tenant == "otto", UnsubOutbox.email == r.email)
        )
    ).scalars().all()
    assert len(outbox) == 1

    await db_session.refresh(r)
    assert r.status == "suppressed"


async def test_record_unsub_idempotent(db_session):
    r = await _seed_recipient(db_session, f"unsub-db-{uuid.uuid4()}@cadencetests.io")

    await udb.record_unsub("otto", str(r.id), r.email)
    await udb.record_unsub("otto", str(r.id), r.email)  # must not raise

    outbox = (
        await db_session.execute(
            select(UnsubOutbox).where(UnsubOutbox.tenant == "otto", UnsubOutbox.email == r.email)
        )
    ).scalars().all()
    assert len(outbox) == 1  # hour-guard: second call adds nothing


async def test_ping():
    assert await udb.ping() is True
