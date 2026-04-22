"""Tests for POST /api/campaigns/{id}/generate."""

from __future__ import annotations

import uuid


def _campaign_body(**overrides) -> dict:
    body = {
        "name": f"test-campaign-{uuid.uuid4()}",
        "tenant": "otto",
        "vertical": "auto",
        "ai_brief": "Help Texans save on auto insurance.",
    }
    body.update(overrides)
    return body


def _recipient_body(email: str, **overrides) -> dict:
    body = {
        "email": email,
        "external_id": "otto_test",
        "vertical": "auto",
        "consent_source": "otto_form",
        "consent_at": "2026-04-21T14:23:00Z",
        "attributes": {"first_name": "Sarah", "state": "TX", "age_band": "35-44"},
    }
    body.update(overrides)
    return body


async def test_generate_endpoint_returns_counts(client, auth_headers):
    c = await client.post(
        "/api/campaigns", json=_campaign_body(), headers=auth_headers
    )
    assert c.status_code == 201
    cid = c.json()["id"]

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await client.post(
        "/api/ingest/recipients", json=_recipient_body(email), headers=auth_headers
    )
    assert r.status_code == 201

    resp = await client.post(
        f"/api/campaigns/{cid}/generate", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "cohorts_processed" in data
    assert "templates_created" in data
    assert "variants_total" in data
    assert "errors" in data


async def test_generate_endpoint_404_missing(client, auth_headers):
    resp = await client.post(
        f"/api/campaigns/{uuid.uuid4()}/generate", headers=auth_headers
    )
    assert resp.status_code == 404


async def test_generate_endpoint_requires_auth(client):
    resp = await client.post(f"/api/campaigns/{uuid.uuid4()}/generate")
    assert resp.status_code == 401
