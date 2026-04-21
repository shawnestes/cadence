"""Tests for POST /api/ingest/recipients."""

from __future__ import annotations

import uuid


def _email() -> str:
    return f"test-{uuid.uuid4()}@cadencetests.io"


def _payload(email: str, **overrides) -> dict:
    body = {
        "email": email,
        "external_id": "otto_lead_1",
        "vertical": "auto",
        "consent_source": "otto_form",
        "consent_at": "2026-04-21T14:23:00Z",
        "attributes": {"first_name": "Sarah", "state": "TX"},
    }
    body.update(overrides)
    return body


async def test_ingest_fresh_email_creates_recipient(client, auth_headers):
    email = _email()
    resp = await client.post(
        "/api/ingest/recipients",
        json=_payload(email),
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"].lower() == email.lower()
    assert body["created"] is True
    assert body["status"] == "active"
    assert body["id"]


async def test_ingest_repeat_email_merges_attributes(client, auth_headers):
    email = _email()
    first = await client.post(
        "/api/ingest/recipients",
        json=_payload(email, attributes={"first_name": "Sarah", "state": "TX"}),
        headers=auth_headers,
    )
    assert first.status_code == 201
    first_id = first.json()["id"]

    second = await client.post(
        "/api/ingest/recipients",
        json=_payload(email, attributes={"age_band": "35-44"}),
        headers=auth_headers,
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["created"] is False
    assert body["id"] == first_id


async def test_ingest_missing_vertical_is_422(client, auth_headers):
    bad = _payload(_email())
    bad.pop("vertical")
    resp = await client.post("/api/ingest/recipients", json=bad, headers=auth_headers)
    assert resp.status_code == 422


async def test_ingest_bad_vertical_value_is_422(client, auth_headers):
    bad = _payload(_email(), vertical="boat")
    resp = await client.post("/api/ingest/recipients", json=bad, headers=auth_headers)
    assert resp.status_code == 422


async def test_ingest_requires_api_key(client):
    resp = await client.post("/api/ingest/recipients", json=_payload(_email()))
    assert resp.status_code == 401
