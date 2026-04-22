"""Tests for /api/templates."""

from __future__ import annotations

import uuid


async def _make_campaign(client, auth_headers, name: str | None = None) -> str:
    payload = {
        "name": name or f"test-campaign-{uuid.uuid4()}",
        "tenant": "otto",
        "vertical": "auto",
    }
    resp = await client.post("/api/campaigns", json=payload, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _template_body(campaign_id: str, **overrides) -> dict:
    body = {
        "campaign_id": campaign_id,
        "tenant": "otto",
        "cohort_key": f"test-cohort-{uuid.uuid4()}",
        "variant_label": "A",
        "subject": "Hi",
        "body_html": "<p>Hi</p>",
        "body_text": "Hi",
    }
    body.update(overrides)
    return body


async def test_create_template_returns_201(client, auth_headers):
    campaign_id = await _make_campaign(client, auth_headers)
    resp = await client.post(
        "/api/templates",
        json=_template_body(campaign_id),
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["campaign_id"] == campaign_id
    assert body["status"] == "generated"


async def test_duplicate_template_returns_409(client, auth_headers):
    campaign_id = await _make_campaign(client, auth_headers)
    body = _template_body(campaign_id)

    first = await client.post("/api/templates", json=body, headers=auth_headers)
    assert first.status_code == 201
    second = await client.post("/api/templates", json=body, headers=auth_headers)
    assert second.status_code == 409


async def test_list_templates_filters_by_campaign(client, auth_headers):
    c1 = await _make_campaign(client, auth_headers)
    c2 = await _make_campaign(client, auth_headers)

    r1 = await client.post(
        "/api/templates", json=_template_body(c1), headers=auth_headers
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/api/templates", json=_template_body(c2), headers=auth_headers
    )
    assert r2.status_code == 201

    resp = await client.get(
        "/api/templates", params={"campaign_id": c1}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    ids = {t["campaign_id"] for t in data["templates"]}
    assert ids == {c1}


async def test_patch_template_updates_status(client, auth_headers):
    campaign_id = await _make_campaign(client, auth_headers)
    create = await client.post(
        "/api/templates",
        json=_template_body(campaign_id),
        headers=auth_headers,
    )
    tid = create.json()["id"]
    resp = await client.patch(
        f"/api/templates/{tid}",
        json={"status": "approved"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


async def test_templates_requires_auth(client):
    resp = await client.get(
        "/api/templates", params={"campaign_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 401
