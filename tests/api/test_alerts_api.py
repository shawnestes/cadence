"""Tests for GET /api/alerts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.core.db import async_session_factory
from app.models.alert import Alert
from app.models.base import uuid7


async def _seed_alert(status: str = "active", severity: str = "critical", metric: str = "bounce_rate") -> Alert:
    now = datetime.now(timezone.utc)
    a = Alert(
        id=uuid7(),
        tenant="otto",
        metric=metric,
        severity=severity,
        threshold=0.05,
        value=0.08,
        status=status,
        pending_since=now,
        activated_at=now if status == "active" else None,
        resolved_at=now if status == "resolved" else None,
        notes=None,
    )
    async with async_session_factory() as session:
        session.add(a)
        await session.commit()
        await session.refresh(a)
    return a


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_alerts_returns_active_by_default(client: AsyncClient, auth_headers: dict):
    """Default (no status filter) returns only pending/active alerts."""
    active = await _seed_alert(status="active")
    resolved = await _seed_alert(status="resolved")

    resp = await client.get("/api/alerts", params={"tenant": "otto"}, headers=auth_headers)
    assert resp.status_code == 200

    data = resp.json()
    ids = [a["id"] for a in data["alerts"]]
    assert str(active.id) in ids
    assert str(resolved.id) not in ids


@pytest.mark.asyncio
async def test_alerts_filter_by_status_resolved(client: AsyncClient, auth_headers: dict):
    """status=resolved filter returns only resolved alerts."""
    active = await _seed_alert(status="active")
    resolved = await _seed_alert(status="resolved")

    resp = await client.get(
        "/api/alerts",
        params={"tenant": "otto", "status": "resolved"},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    data = resp.json()
    ids = [a["id"] for a in data["alerts"]]
    assert str(resolved.id) in ids
    assert str(active.id) not in ids


@pytest.mark.asyncio
async def test_alerts_ordered_by_created_at_desc(client: AsyncClient, auth_headers: dict):
    """Results come back newest first."""
    a1 = await _seed_alert(status="active", metric="bounce_rate")
    a2 = await _seed_alert(status="pending", metric="complaint_rate")

    resp = await client.get(
        "/api/alerts",
        params={"tenant": "otto", "status": "pending"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    # a2 was seeded after a1; with desc order a2 should come first
    pending_ids = [a["id"] for a in data["alerts"]]
    assert str(a2.id) in pending_ids


@pytest.mark.asyncio
async def test_alerts_total_count(client: AsyncClient, auth_headers: dict):
    """total reflects count of matching rows."""
    await _seed_alert(status="active")
    await _seed_alert(status="active")
    await _seed_alert(status="resolved")

    resp = await client.get(
        "/api/alerts",
        params={"tenant": "otto"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2


@pytest.mark.asyncio
async def test_alerts_pagination(client: AsyncClient, auth_headers: dict):
    """limit and offset work correctly."""
    await _seed_alert(status="active", metric="bounce_rate")
    await _seed_alert(status="active", metric="complaint_rate")

    resp = await client.get(
        "/api/alerts",
        params={"tenant": "otto", "limit": 1, "offset": 0},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["alerts"]) == 1


@pytest.mark.asyncio
async def test_alerts_requires_auth(client: AsyncClient):
    """Returns 401 without API key."""
    resp = await client.get("/api/alerts", params={"tenant": "otto"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_alert_response_shape(client: AsyncClient, auth_headers: dict):
    """Response has expected fields."""
    await _seed_alert(status="active")

    resp = await client.get("/api/alerts", params={"tenant": "otto"}, headers=auth_headers)
    assert resp.status_code == 200

    alert = resp.json()["alerts"][0]
    for field in ("id", "tenant", "metric", "severity", "threshold", "value", "status",
                  "pending_since", "activated_at", "resolved_at", "notes", "created_at", "updated_at"):
        assert field in alert, f"missing field: {field}"
