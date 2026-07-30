"""unsub_server HTTP behavior, exercised over ASGITransport against the
cadence test DB. Tokens are signed with cadence's signer -- full-loop
contract: cadence signs, unsub_server verifies and writes."""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.db import engine as cadence_engine
from app.core.unsub import UnsubPayload, UnsubSigner
from app.models.recipient import Recipient
from app.models.suppression import Suppression

KEY = "unsub-server-route-test-key"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from unsub_server import db as udb

    monkeypatch.setenv(
        "UNSUB_DATABASE_URL", cadence_engine.url.render_as_string(hide_password=False)
    )
    monkeypatch.setenv("CADENCE_UNSUB_SIGNING_KEY", KEY)
    udb.reset_engine()
    yield
    udb.reset_engine()


@pytest.fixture
async def unsub_client():
    from unsub_server.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://unsub.test") as c:
        yield c


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


def _token(recipient_id: str) -> str:
    signer = UnsubSigner(signing_key=KEY, base_url="http://x")
    return signer.sign(UnsubPayload(recipient_id=recipient_id, send_id=None, tenant="otto"))


async def test_get_renders_confirm_page(unsub_client, db_session):
    r = await _seed_recipient(db_session, f"unsub-rt-{uuid.uuid4()}@cadencetests.io")
    resp = await unsub_client.get(f"/u/{_token(str(r.id))}")
    assert resp.status_code == 200
    assert r.email in resp.text
    assert "form" in resp.text and "POST" in resp.text.upper()


async def test_get_bad_token_410(unsub_client):
    resp = await unsub_client.get("/u/garbage")
    assert resp.status_code == 410


async def test_post_unsubscribes(unsub_client, db_session):
    r = await _seed_recipient(db_session, f"unsub-rt-{uuid.uuid4()}@cadencetests.io")
    resp = await unsub_client.post(f"/u/{_token(str(r.id))}")
    assert resp.status_code == 200
    assert "Unsubscribed" in resp.text

    sup = (
        await db_session.execute(
            select(Suppression).where(Suppression.tenant == "otto", Suppression.email == r.email)
        )
    ).scalar_one()
    assert sup.source == "one_click"
    await db_session.refresh(r)
    assert r.status == "suppressed"


async def test_post_bad_token_410(unsub_client):
    resp = await unsub_client.post("/u/garbage")
    assert resp.status_code == 410


async def test_post_unknown_recipient_shows_done_page_and_records_diagnostic(
    unsub_client, db_session
):
    from app.models.unsub_diagnostic import UnsubDiagnostic

    ghost_id = str(uuid.uuid4())
    resp = await unsub_client.post(f"/u/{_token(ghost_id)}")

    assert resp.status_code == 200
    assert "unsubscribed" in resp.text.lower()
    diag = (
        await db_session.execute(
            select(UnsubDiagnostic).where(
                UnsubDiagnostic.recipient_id == uuid.UUID(ghost_id)
            )
        )
    ).scalar_one_or_none()
    assert diag is not None
    assert diag.reason == "unknown_recipient"


async def test_get_unknown_recipient_410(unsub_client):
    resp = await unsub_client.get(f"/u/{_token(str(uuid.uuid4()))}")
    assert resp.status_code == 410


async def test_health(unsub_client):
    resp = await unsub_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
