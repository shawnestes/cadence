"""Tests for /api/campaigns."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.send import Send


def _campaign_body(**overrides) -> dict:
    body = {
        "name": f"test-campaign-{uuid.uuid4()}",
        "tenant": "otto",
        "vertical": "auto",
    }
    body.update(overrides)
    return body


def _template_body(campaign_id: str, **overrides) -> dict:
    body = {
        "campaign_id": campaign_id,
        "tenant": "otto",
        "cohort_key": f"test-cohort-{uuid.uuid4()}",
        "variant_label": "A",
        "subject": "Hi {{ first_name }}",
        "body_html": "<p>Hi {{ first_name }}</p>",
        "body_text": "Hi {{ first_name }}",
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
        "attributes": {"first_name": "Sarah", "state": "TX"},
    }
    body.update(overrides)
    return body


async def test_create_campaign_returns_201(client, auth_headers):
    resp = await client.post(
        "/api/campaigns", json=_campaign_body(), headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["id"]
    assert data["status"] == "draft"


async def test_list_campaigns_filters_by_status(client, auth_headers):
    c1 = await client.post(
        "/api/campaigns", json=_campaign_body(), headers=auth_headers
    )
    c2 = await client.post(
        "/api/campaigns", json=_campaign_body(), headers=auth_headers
    )
    assert c1.status_code == 201 and c2.status_code == 201

    # Patch one to active
    patch = await client.patch(
        f"/api/campaigns/{c1.json()['id']}",
        json={"status": "active"},
        headers=auth_headers,
    )
    assert patch.status_code == 200

    resp = await client.get(
        "/api/campaigns", params={"status": "active"}, headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    ids = {c["id"] for c in data["campaigns"]}
    assert c1.json()["id"] in ids
    assert c2.json()["id"] not in ids


async def test_get_campaign_returns_404_for_missing(client, auth_headers):
    resp = await client.get(
        f"/api/campaigns/{uuid.uuid4()}", headers=auth_headers
    )
    assert resp.status_code == 404


async def test_patch_campaign_updates_fields(client, auth_headers):
    create = await client.post(
        "/api/campaigns", json=_campaign_body(), headers=auth_headers
    )
    cid = create.json()["id"]
    new_name = f"test-renamed-{uuid.uuid4()}"
    resp = await client.patch(
        f"/api/campaigns/{cid}", json={"name": new_name}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == new_name


async def test_send_with_no_approved_template(client, auth_headers):
    # campaign + recipient but no template
    c = await client.post(
        "/api/campaigns", json=_campaign_body(), headers=auth_headers
    )
    cid = c.json()["id"]
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await client.post(
        "/api/ingest/recipients",
        json=_recipient_body(email),
        headers=auth_headers,
    )
    assert r.status_code == 201
    rid = r.json()["id"]

    resp = await client.post(
        f"/api/campaigns/{cid}/send",
        json={"recipient_ids": [rid], "tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["queued"] == 0
    assert data["skipped_no_template"] == 1


async def test_send_with_approved_template_enqueues(
    client, auth_headers, db_session
):
    c = await client.post(
        "/api/campaigns", json=_campaign_body(), headers=auth_headers
    )
    cid = c.json()["id"]

    t = await client.post(
        "/api/templates",
        json=_template_body(cid),
        headers=auth_headers,
    )
    assert t.status_code == 201
    tid = t.json()["id"]

    patch = await client.patch(
        f"/api/templates/{tid}",
        json={"status": "approved"},
        headers=auth_headers,
    )
    assert patch.status_code == 200

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await client.post(
        "/api/ingest/recipients",
        json=_recipient_body(email),
        headers=auth_headers,
    )
    assert r.status_code == 201
    rid = r.json()["id"]

    resp = await client.post(
        f"/api/campaigns/{cid}/send",
        json={"recipient_ids": [rid], "tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["queued"] == 1

    rows = (
        await db_session.execute(
            select(Send).where(Send.recipient_id == uuid.UUID(rid))
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "queued"


async def test_send_endpoint_requires_auth(client):
    resp = await client.post(
        f"/api/campaigns/{uuid.uuid4()}/send",
        json={"recipient_ids": [str(uuid.uuid4())], "tenant": "otto"},
    )
    assert resp.status_code == 401


async def test_send_rejects_zero_selectors(client, auth_headers):
    c = await client.post("/api/campaigns", json=_campaign_body(), headers=auth_headers)
    cid = c.json()["id"]
    resp = await client.post(
        f"/api/campaigns/{cid}/send",
        json={"tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


async def test_send_rejects_more_than_one_selector(client, auth_headers):
    c = await client.post("/api/campaigns", json=_campaign_body(), headers=auth_headers)
    cid = c.json()["id"]
    resp = await client.post(
        f"/api/campaigns/{cid}/send",
        json={
            "recipient_ids": [str(uuid.uuid4())],
            "email_list": ["a@cadencetests.io"],
            "tenant": "otto",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400


async def test_send_rejects_all_three_selectors(client, auth_headers):
    c = await client.post("/api/campaigns", json=_campaign_body(), headers=auth_headers)
    cid = c.json()["id"]
    resp = await client.post(
        f"/api/campaigns/{cid}/send",
        json={
            "recipient_ids": [str(uuid.uuid4())],
            "email_list": ["a@cadencetests.io"],
            "segment": {"vertical": "auto"},
            "tenant": "otto",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400


async def test_send_by_segment_enqueues_only_matching_recipients(
    client, auth_headers, db_session
):
    c = await client.post("/api/campaigns", json=_campaign_body(), headers=auth_headers)
    cid = c.json()["id"]

    t = await client.post("/api/templates", json=_template_body(cid), headers=auth_headers)
    assert t.status_code == 201
    tid = t.json()["id"]
    patch = await client.patch(
        f"/api/templates/{tid}", json={"status": "approved"}, headers=auth_headers
    )
    assert patch.status_code == 200

    matching_email = f"test-{uuid.uuid4()}@cadencetests.io"
    r_match = await client.post(
        "/api/ingest/recipients",
        json=_recipient_body(matching_email, attributes={"state": "TX"}),
        headers=auth_headers,
    )
    assert r_match.status_code == 201
    matching_id = r_match.json()["id"]

    non_matching_email = f"test-{uuid.uuid4()}@cadencetests.io"
    r_nomatch = await client.post(
        "/api/ingest/recipients",
        json=_recipient_body(non_matching_email, attributes={"state": "CA"}),
        headers=auth_headers,
    )
    assert r_nomatch.status_code == 201

    resp = await client.post(
        f"/api/campaigns/{cid}/send",
        json={"segment": {"attributes.state": "TX"}, "tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["queued"] == 1

    rows = (
        await db_session.execute(select(Send).where(Send.campaign_id == uuid.UUID(cid)))
    ).scalars().all()
    assert len(rows) == 1
    assert str(rows[0].recipient_id) == matching_id


async def test_send_by_invalid_segment_returns_400(client, auth_headers):
    c = await client.post("/api/campaigns", json=_campaign_body(), headers=auth_headers)
    cid = c.json()["id"]

    resp = await client.post(
        f"/api/campaigns/{cid}/send",
        json={"segment": {"all": "not-a-list"}, "tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 400
