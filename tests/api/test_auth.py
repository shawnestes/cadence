"""Tests for API key auth middleware on /api/* routes."""

from __future__ import annotations


async def test_suppressions_missing_header_is_unauthorized(client):
    resp = await client.get("/api/suppressions")
    assert resp.status_code == 401


async def test_suppressions_wrong_key_is_unauthorized(client):
    resp = await client.get("/api/suppressions", headers={"X-API-Key": "nope"})
    assert resp.status_code == 401


async def test_suppressions_correct_key_is_ok(client, auth_headers):
    resp = await client.get("/api/suppressions", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "suppressions" in body
    assert "total" in body
    assert isinstance(body["suppressions"], list)
    assert isinstance(body["total"], int)


async def test_health_requires_no_api_key(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_metrics_requires_no_api_key(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
