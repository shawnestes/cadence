"""Tests for /api/sending-identities (SPEC_ADDENDUM_003 §8)."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text


def _name(prefix: str = "identity") -> str:
    return f"test-{prefix}-{uuid.uuid4()}"


def _payload(**overrides) -> dict:
    body = {
        "tenant": "otto",
        "name": _name(),
        "esp_provider": "ses",
        "from_address": "alerts@example.com",
        "sending_domain": "example.com",
        "credentials_ref": "otto/ses/primary",
    }
    body.update(overrides)
    return body


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_sending_identities(db_session):
    # sending_identities isn't covered by the shared purge fixture, so each
    # test cleans its own 'test-' rows to keep reruns green. Tests that flip
    # is_default may clobber the tenant's seeded default identity along the
    # way, so remember it here and restore it after the row cleanup.
    original_default_id = (
        await db_session.execute(
            text(
                "SELECT id FROM sending_identities "
                "WHERE tenant = 'otto' AND is_default IS TRUE"
            )
        )
    ).scalar_one_or_none()

    yield

    await db_session.execute(
        text("DELETE FROM sending_identities WHERE name LIKE 'test-%'")
    )
    if original_default_id is not None:
        await db_session.execute(
            text(
                "UPDATE sending_identities SET is_default = TRUE WHERE id = :id"
            ),
            {"id": original_default_id},
        )
    await db_session.commit()


async def test_create_sending_identity_returns_201(client, auth_headers):
    resp = await client.post(
        "/api/sending-identities", json=_payload(), headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["esp_provider"] == "ses"
    assert body["status"] == "active"
    assert body["is_default"] is False
    assert body["id"]


async def test_list_sending_identities_filters_by_tenant_and_status(client, auth_headers):
    name = _name()
    resp = await client.post(
        "/api/sending-identities",
        json=_payload(name=name, status="paused"),
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text

    listed = await client.get(
        "/api/sending-identities",
        params={"tenant": "otto", "status": "paused"},
        headers=auth_headers,
    )
    assert listed.status_code == 200, listed.text
    names = [i["name"] for i in listed.json()["sending_identities"]]
    assert name in names

    listed_active = await client.get(
        "/api/sending-identities",
        params={"tenant": "otto", "status": "active"},
        headers=auth_headers,
    )
    assert name not in [i["name"] for i in listed_active.json()["sending_identities"]]


async def test_patch_sending_identity_updates_status(client, auth_headers):
    created = await client.post(
        "/api/sending-identities", json=_payload(), headers=auth_headers
    )
    identity_id = created.json()["id"]

    patched = await client.patch(
        f"/api/sending-identities/{identity_id}",
        json={"status": "paused"},
        headers=auth_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "paused"


async def test_duplicate_name_returns_409(client, auth_headers):
    body = _payload()
    first = await client.post("/api/sending-identities", json=body, headers=auth_headers)
    assert first.status_code == 201, first.text

    second = await client.post("/api/sending-identities", json=body, headers=auth_headers)
    assert second.status_code == 409


async def test_invalid_esp_provider_returns_422(client, auth_headers):
    resp = await client.post(
        "/api/sending-identities",
        json=_payload(esp_provider="mailgun"),
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_invalid_from_address_returns_422(client, auth_headers):
    resp = await client.post(
        "/api/sending-identities",
        json=_payload(from_address="not-an-email"),
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "bad_key", ["api_key", "secret_token", "password", "SECRET_VALUE"]
)
async def test_secret_looking_provider_config_key_returns_422(
    client, auth_headers, bad_key
):
    resp = await client.post(
        "/api/sending-identities",
        json=_payload(provider_config={bad_key: "shh"}),
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "credentials_ref" in resp.text


async def test_secret_looking_provider_config_key_rejected_on_patch(
    client, auth_headers
):
    created = await client.post(
        "/api/sending-identities", json=_payload(), headers=auth_headers
    )
    identity_id = created.json()["id"]

    patched = await client.patch(
        f"/api/sending-identities/{identity_id}",
        json={"provider_config": {"api_key": "shh"}},
        headers=auth_headers,
    )
    assert patched.status_code == 422


async def test_default_switch_is_atomic(client, auth_headers):
    first = await client.post(
        "/api/sending-identities",
        json=_payload(is_default=True),
        headers=auth_headers,
    )
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]
    assert first.json()["is_default"] is True

    second = await client.post(
        "/api/sending-identities",
        json=_payload(is_default=True),
        headers=auth_headers,
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    assert second.json()["is_default"] is True

    listed = await client.get(
        "/api/sending-identities",
        params={"tenant": "otto", "limit": 1000},
        headers=auth_headers,
    )
    defaults = [
        i
        for i in listed.json()["sending_identities"]
        if i["id"] in (first_id, second_id)
    ]
    assert len([i for i in defaults if i["is_default"]]) == 1

    first_after = await client.get(
        f"/api/sending-identities/{first_id}", headers=auth_headers
    )
    assert first_after.json()["is_default"] is False


async def test_default_switch_via_patch_is_atomic(client, auth_headers):
    first = await client.post(
        "/api/sending-identities",
        json=_payload(is_default=True),
        headers=auth_headers,
    )
    first_id = first.json()["id"]

    second = await client.post(
        "/api/sending-identities", json=_payload(), headers=auth_headers
    )
    second_id = second.json()["id"]

    patched = await client.patch(
        f"/api/sending-identities/{second_id}",
        json={"is_default": True},
        headers=auth_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["is_default"] is True

    first_after = await client.get(
        f"/api/sending-identities/{first_id}", headers=auth_headers
    )
    assert first_after.json()["is_default"] is False


async def test_requires_api_key(client):
    resp = await client.get("/api/sending-identities", params={"tenant": "otto"})
    assert resp.status_code == 401

    resp = await client.post("/api/sending-identities", json=_payload())
    assert resp.status_code == 401
