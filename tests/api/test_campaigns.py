"""Tests for /api/campaigns."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.campaign import Campaign
from app.models.send import Send
from tests._identity_helpers import create_identity, get_or_create_default_identity
from tests.api._contact_profile_seed import seed_contact_profile


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
    await seed_contact_profile(matching_id, email_domain="tx.cadencetests.io")

    non_matching_email = f"test-{uuid.uuid4()}@cadencetests.io"
    r_nomatch = await client.post(
        "/api/ingest/recipients",
        json=_recipient_body(non_matching_email, attributes={"state": "CA"}),
        headers=auth_headers,
    )
    assert r_nomatch.status_code == 201
    await seed_contact_profile(r_nomatch.json()["id"], email_domain="ca.cadencetests.io")

    resp = await client.post(
        f"/api/campaigns/{cid}/send",
        json={"segment": {"email_domain": "tx.cadencetests.io"}, "tenant": "otto"},
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


# --- Sending identities (SPEC_ADDENDUM_003) ----------------------------------


async def test_create_campaign_stamps_explicit_sending_identity_id(
    client, auth_headers, db_session
):
    identity = await create_identity(db_session)

    resp = await client.post(
        "/api/campaigns",
        json=_campaign_body(sending_identity_id=str(identity.id)),
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["sending_identity_id"] == str(identity.id)

    row = (
        await db_session.execute(select(Campaign).where(Campaign.id == uuid.UUID(resp.json()["id"])))
    ).scalar_one()
    assert row.sending_identity_id == identity.id


async def test_send_stamps_tenant_default_identity(client, auth_headers, db_session):
    default_identity = await get_or_create_default_identity(db_session, tenant="otto")

    c = await client.post("/api/campaigns", json=_campaign_body(), headers=auth_headers)
    cid = c.json()["id"]
    t = await client.post("/api/templates", json=_template_body(cid), headers=auth_headers)
    tid = t.json()["id"]
    await client.patch(
        f"/api/templates/{tid}", json={"status": "approved"}, headers=auth_headers
    )

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await client.post(
        "/api/ingest/recipients", json=_recipient_body(email), headers=auth_headers
    )
    rid = r.json()["id"]

    resp = await client.post(
        f"/api/campaigns/{cid}/send",
        json={"recipient_ids": [rid], "tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued"] == 1

    rows = (
        await db_session.execute(select(Send).where(Send.recipient_id == uuid.UUID(rid)))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].sending_identity_id == default_identity.id
    assert rows[0].esp == default_identity.esp_provider


async def test_send_explicit_campaign_identity_wins_over_tenant_default(
    client, auth_headers, db_session
):
    await get_or_create_default_identity(db_session, tenant="otto")
    explicit_identity = await create_identity(db_session, tenant="otto")

    c = await client.post("/api/campaigns", json=_campaign_body(), headers=auth_headers)
    cid = c.json()["id"]
    patch = await client.patch(
        f"/api/campaigns/{cid}",
        json={"sending_identity_id": str(explicit_identity.id)},
        headers=auth_headers,
    )
    assert patch.status_code == 200

    t = await client.post("/api/templates", json=_template_body(cid), headers=auth_headers)
    tid = t.json()["id"]
    await client.patch(
        f"/api/templates/{tid}", json={"status": "approved"}, headers=auth_headers
    )

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await client.post(
        "/api/ingest/recipients", json=_recipient_body(email), headers=auth_headers
    )
    rid = r.json()["id"]

    resp = await client.post(
        f"/api/campaigns/{cid}/send",
        json={"recipient_ids": [rid], "tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued"] == 1

    rows = (
        await db_session.execute(select(Send).where(Send.recipient_id == uuid.UUID(rid)))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].sending_identity_id == explicit_identity.id


async def test_send_with_paused_identity_leaves_sends_queued_not_failed(
    client, auth_headers, db_session
):
    paused_identity = await create_identity(db_session, tenant="otto", status="paused")

    c = await client.post("/api/campaigns", json=_campaign_body(), headers=auth_headers)
    cid = c.json()["id"]
    patch = await client.patch(
        f"/api/campaigns/{cid}",
        json={"sending_identity_id": str(paused_identity.id)},
        headers=auth_headers,
    )
    assert patch.status_code == 200

    t = await client.post("/api/templates", json=_template_body(cid), headers=auth_headers)
    tid = t.json()["id"]
    await client.patch(
        f"/api/templates/{tid}", json={"status": "approved"}, headers=auth_headers
    )

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await client.post(
        "/api/ingest/recipients", json=_recipient_body(email), headers=auth_headers
    )
    rid = r.json()["id"]

    resp = await client.post(
        f"/api/campaigns/{cid}/send",
        json={"recipient_ids": [rid], "tenant": "otto"},
        headers=auth_headers,
    )
    # Never fails the request just because the identity is paused.
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued"] == 1

    rows = (
        await db_session.execute(select(Send).where(Send.recipient_id == uuid.UUID(rid)))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "queued"
    assert rows[0].sending_identity_id == paused_identity.id
