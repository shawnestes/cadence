"""Tests for POST /api/segments/preview."""

from __future__ import annotations

import uuid

from tests.api._contact_profile_seed import seed_contact_profile


def _email() -> str:
    return f"test-{uuid.uuid4()}@cadencetests.io"


def _recipient_payload(email: str, **overrides) -> dict:
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


async def _ingest(client, auth_headers, **overrides) -> str:
    resp = await client.post(
        "/api/ingest/recipients",
        json=_recipient_payload(_email(), **overrides),
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_preview_counts_and_samples_matching_recipients(client, auth_headers):
    tx_ids = set()
    for _ in range(3):
        rid = await _ingest(client, auth_headers)
        await seed_contact_profile(rid, email_domain="tx.cadencetests.io")
        tx_ids.add(rid)
    ca_id = await _ingest(client, auth_headers)
    await seed_contact_profile(ca_id, email_domain="ca.cadencetests.io")

    resp = await client.post(
        "/api/segments/preview",
        json={"segment": {"email_domain": "tx.cadencetests.io"}, "tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["count"] >= 3
    sample_ids = {r["contact_id"] for r in data["sample"]}
    assert tx_ids.issubset(sample_ids) or len(data["sample"]) == 10
    for row in data["sample"]:
        assert set(row) == {
            "contact_id",
            "email_domain",
            "mailbox_provider",
            "engagement_tier",
            "engagement_score",
        }


async def test_preview_v2_dsl_all_operator(client, auth_headers):
    rid = await _ingest(client, auth_headers)
    await seed_contact_profile(
        rid, email_domain="ok.cadencetests.io", mailbox_provider=1
    )

    seg = {
        "all": [
            {"field": "email_domain", "eq": "ok.cadencetests.io"},
            {"field": "mailbox_provider", "in": ["gmail", "yahoo"]},
        ]
    }
    resp = await client.post(
        "/api/segments/preview",
        json={"segment": seg, "tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    ids = {r["contact_id"] for r in resp.json()["sample"]}
    # rid should be included if within the first 10 sampled; assert via count instead
    assert resp.json()["count"] >= 1
    if resp.json()["count"] <= 10:
        assert rid in ids


async def test_preview_no_match_returns_zero(client, auth_headers):
    resp = await client.post(
        "/api/segments/preview",
        json={"segment": {"email_domain": "zz-impossible.cadencetests.io"}, "tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["sample"] == []


async def test_preview_invalid_segment_returns_400(client, auth_headers):
    resp = await client.post(
        "/api/segments/preview",
        json={"segment": {"all": "not-a-list"}, "tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


async def test_preview_requires_auth(client):
    resp = await client.post(
        "/api/segments/preview",
        json={"segment": None, "tenant": "otto"},
    )
    assert resp.status_code == 401


async def test_preview_null_segment_matches_all(client, auth_headers):
    rid = await _ingest(client, auth_headers)
    await seed_contact_profile(rid, email_domain="null-match.cadencetests.io")
    resp = await client.post(
        "/api/segments/preview",
        json={"segment": None, "tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1
