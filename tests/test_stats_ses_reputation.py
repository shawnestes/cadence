"""Tests for GET /api/stats/ses-reputation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

_MOCK_ACCOUNT = {
    "SendingEnabled": True,
    "ProductionAccessEnabled": True,
    "EnforcementStatus": None,
    "SendQuota": {
        "Max24HourSend": 50000.0,
        "SentLast24Hours": 1234.0,
        "MaxSendRate": 14.0,
    },
    "Details": {},
}


@pytest.mark.asyncio
async def test_ses_reputation_happy_path(client: AsyncClient, auth_headers: dict):
    """Returns expected shape when SES responds successfully."""
    with patch("app.api.stats.asyncio.to_thread", new=AsyncMock(return_value=_MOCK_ACCOUNT)):
        resp = await client.get(
            "/api/stats/ses-reputation",
            params={"tenant": "otto"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["tenant"] == "otto"
    assert data["sending_enabled"] is True
    assert data["production_access_enabled"] is True
    assert data["enforcement_status"] is None
    assert data["max_24_hour_send"] == 50000.0
    assert data["sent_last_24_hours"] == 1234.0
    assert data["max_send_rate"] == 14.0
    assert data["reputation_sending_enabled"] is None
    assert "fetched_at" in data


@pytest.mark.asyncio
async def test_ses_reputation_with_review_details(client: AsyncClient, auth_headers: dict):
    """reputation_sending_enabled is populated when ReviewDetails is present."""
    account = {
        **_MOCK_ACCOUNT,
        "Details": {
            "ReviewDetails": {
                "SendingEnabled": False,
            }
        },
    }
    with patch("app.api.stats.asyncio.to_thread", new=AsyncMock(return_value=account)):
        resp = await client.get(
            "/api/stats/ses-reputation",
            params={"tenant": "otto"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert resp.json()["reputation_sending_enabled"] is False


@pytest.mark.asyncio
async def test_ses_reputation_tenant_echoed(client: AsyncClient, auth_headers: dict):
    """tenant query param is echoed back in the response."""
    with patch("app.api.stats.asyncio.to_thread", new=AsyncMock(return_value=_MOCK_ACCOUNT)):
        resp = await client.get(
            "/api/stats/ses-reputation",
            params={"tenant": "acme"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert resp.json()["tenant"] == "acme"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ses_reputation_no_credentials(client: AsyncClient, auth_headers: dict):
    """Returns 503 when AWS credentials are not configured."""
    with patch(
        "app.api.stats.asyncio.to_thread",
        new=AsyncMock(side_effect=NoCredentialsError()),
    ):
        resp = await client.get(
            "/api/stats/ses-reputation",
            params={"tenant": "otto"},
            headers=auth_headers,
        )

    assert resp.status_code == 503
    assert "credentials" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_ses_reputation_client_error(client: AsyncClient, auth_headers: dict):
    """Returns 503 when SES returns a ClientError."""
    error_response = {
        "Error": {
            "Code": "AccessDeniedException",
            "Message": "User is not authorized to call ses:GetAccount",
        }
    }
    exc = ClientError(error_response, "GetAccount")

    with patch(
        "app.api.stats.asyncio.to_thread",
        new=AsyncMock(side_effect=exc),
    ):
        resp = await client.get(
            "/api/stats/ses-reputation",
            params={"tenant": "otto"},
            headers=auth_headers,
        )

    assert resp.status_code == 503
    assert "SES unavailable" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ses_reputation_requires_auth(client: AsyncClient):
    """Returns 401 without API key."""
    resp = await client.get("/api/stats/ses-reputation", params={"tenant": "otto"})
    assert resp.status_code == 401
