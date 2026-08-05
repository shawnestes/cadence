"""POST /api/ingest/records/batch."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.db import async_session_factory

_GOOD_CONSENT = {
    "tcpa_text": "By submitting you agree to receive emails.",
    "tcpa_consent_at": "2026-01-15T10:30:00Z",
}


def _record(email: str, **overrides) -> dict:
    row = {
        "email": email, "user_id": "otto-1", "first_name": "Shawn",
        "last_name": "Estes", "address": "100 Congress Ave", "city": "Austin",
        "state": "TX", "zip": "78701", "phone": "5125551234",
        **_GOOD_CONSENT,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_batch_requires_auth(client):
    resp = await client.post(
        "/api/ingest/records/batch",
        json={"tenant": "otto", "vertical": "auto", "records": []},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_batch_reports_per_row_status(client, auth_headers):
    good = f"api-good-{uuid.uuid4()}@cadencetests.io"
    noconsent = f"api-nc-{uuid.uuid4()}@cadencetests.io"
    resp = await client.post(
        "/api/ingest/records/batch",
        headers=auth_headers,
        json={
            "tenant": "otto",
            "vertical": "auto",
            "records": [
                _record(good),
                _record(noconsent, tcpa_text=""),
                _record("not-an-email"),
            ],
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["status"] for r in results] == ["created", "quarantined", "rejected"]
    assert results[0]["index"] == 0
    assert results[2]["errors"]

    async with async_session_factory() as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM recipients WHERE tenant='otto' AND email=:e"),
                {"e": good},
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_batch_size_is_capped(client, auth_headers):
    resp = await client.post(
        "/api/ingest/records/batch",
        headers=auth_headers,
        json={
            "tenant": "otto",
            "vertical": "auto",
            "records": [_record(f"cap-{n}@cadencetests.io") for n in range(201)],
        },
    )
    assert resp.status_code == 422
