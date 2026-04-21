"""Tests for /api/suppressions CRUD."""

from __future__ import annotations

import uuid


def _email() -> str:
    return f"test-{uuid.uuid4()}@cadencetests.io"


def _payload(email: str, **overrides) -> dict:
    body = {
        "email": email,
        "reason": "manual",
        "source": "admin_user",
        "notes": "test",
    }
    body.update(overrides)
    return body


async def test_create_new_suppression_returns_201(client, auth_headers):
    email = _email()
    resp = await client.post("/api/suppressions", json=_payload(email), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"].lower() == email.lower()
    assert body["reason"] == "manual"
    assert body["source"] == "admin_user"
    assert body["id"]


async def test_create_duplicate_returns_200_with_existing_row(client, auth_headers):
    email = _email()
    first = await client.post("/api/suppressions", json=_payload(email), headers=auth_headers)
    assert first.status_code == 201
    first_id = first.json()["id"]

    second = await client.post(
        "/api/suppressions",
        json=_payload(email, reason="complaint", source="sns"),
        headers=auth_headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first_id


async def test_get_with_email_filter_finds_row(client, auth_headers):
    email = _email()
    await client.post("/api/suppressions", json=_payload(email), headers=auth_headers)

    resp = await client.get(
        "/api/suppressions",
        params={"email": email},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert any(s["email"].lower() == email.lower() for s in body["suppressions"])


async def test_get_without_filter_lists_at_least_one(client, auth_headers):
    email = _email()
    await client.post("/api/suppressions", json=_payload(email), headers=auth_headers)

    resp = await client.get("/api/suppressions", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


async def test_delete_existing_suppression_returns_204(client, auth_headers):
    email = _email()
    await client.post("/api/suppressions", json=_payload(email), headers=auth_headers)

    resp = await client.delete(f"/api/suppressions/{email}", headers=auth_headers)
    assert resp.status_code == 204

    check = await client.get(
        "/api/suppressions", params={"email": email}, headers=auth_headers
    )
    assert check.json()["total"] == 0


async def test_delete_missing_suppression_returns_404(client, auth_headers):
    missing = _email()
    resp = await client.delete(f"/api/suppressions/{missing}", headers=auth_headers)
    assert resp.status_code == 404


async def test_pagination_respects_limit_and_offset(client, auth_headers):
    emails = [_email() for _ in range(3)]
    for e in emails:
        r = await client.post("/api/suppressions", json=_payload(e), headers=auth_headers)
        assert r.status_code == 201

    page1 = await client.get(
        "/api/suppressions",
        params={"limit": 1, "offset": 0},
        headers=auth_headers,
    )
    assert page1.status_code == 200
    body1 = page1.json()
    assert len(body1["suppressions"]) == 1
    assert body1["total"] >= 3

    page2 = await client.get(
        "/api/suppressions",
        params={"limit": 1, "offset": 1},
        headers=auth_headers,
    )
    assert page2.status_code == 200
    body2 = page2.json()
    assert len(body2["suppressions"]) == 1
    assert body1["suppressions"][0]["id"] != body2["suppressions"][0]["id"]


async def test_suppressions_requires_api_key(client):
    assert (await client.get("/api/suppressions")).status_code == 401
    assert (
        await client.post("/api/suppressions", json=_payload(_email()))
    ).status_code == 401
    assert (
        await client.delete(f"/api/suppressions/{_email()}")
    ).status_code == 401
