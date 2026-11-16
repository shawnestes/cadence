"""POST /api/ingest/records/batch."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.db import async_session_factory


@pytest.fixture
def otto_ingest_headers() -> dict[str, str]:
    return {"X-API-Key": "test-otto-ingest-key"}

_GOOD_CONSENT = {
    "tcpa_text": "By submitting you agree to receive emails.",
    "tcpa_date": "2026-01-15T10:30:00Z",
}


def _record(email: str, **overrides) -> dict:
    row = {
        "email": email, "user_id": "otto-1", "first_name": "Shawn",
        "last_name": "Estes", "city": "Austin",
        "state": "TX", "zip": "78701", "phone": "5125551234",
        **_GOOD_CONSENT,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_batch_requires_auth(client):
    resp = await client.post(
        "/api/ingest/records/batch",
        json={"tenant": "otto", "vertical": "auto", "records": []},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_batch_reports_per_row_status(client, otto_ingest_headers):
    good = f"api-good-{uuid.uuid4()}@cadencetests.io"
    noconsent = f"api-nc-{uuid.uuid4()}@cadencetests.io"
    resp = await client.post(
        "/api/ingest/records/batch",
        headers=otto_ingest_headers,
        json={
            "tenant": "otto",
            "vertical": "auto",
            "records": [
                _record(good),
                _record(noconsent, tcpa_text=""),
                _record("not-an-email"),
            ],
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["status"] for r in results] == ["created", "quarantined", "rejected"]
    assert results[0]["index"] == 0
    assert results[2]["errors"]

    async with async_session_factory() as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM recipients WHERE tenant='otto' AND email=:e"),
                {"e": good},
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_batch_survives_a_bad_row_at_the_database_level(client, otto_ingest_headers, monkeypatch):
    """Final-review finding: the batch endpoint used to loop all rows on one
    shared session with a single commit and no per-row isolation, so one
    row that raised at the DB level (e.g. CAST(:ip AS inet) on a malformed
    IP) took the whole batch down with a bare 500 and lost both good rows.

    The genuine DataError reproduction now lives in
    tests/services/test_record_ingest.py, directly below the tenant-mapping
    layer whose normalizer (finding 2) sanitizes malformed IPs before they
    ever reach ingest_record — so that specific value no longer reaches the
    DB from this endpoint. This test instead verifies the endpoint's own
    contract (per-row status, no 500, sibling rows persisted) by forcing a
    failure at the same call site ingest_records_batch uses, the way the
    CLI's own equivalent test does (tests/cli/test_load_records.py::
    test_batch_failure_rolls_back_and_retries_rows_individually).
    """
    import app.api.ingest as ingest_module

    good1 = f"api-sp-good1-{uuid.uuid4()}@cadencetests.io"
    bad = f"api-sp-bad-{uuid.uuid4()}@cadencetests.io"
    good2 = f"api-sp-good2-{uuid.uuid4()}@cadencetests.io"

    real_ingest_one_row = ingest_module.ingest_one_row

    async def _flaky_ingest_one_row(raw, session, **kwargs):
        if raw.get("email") == bad:
            raise RuntimeError("simulated statement failure")
        return await real_ingest_one_row(raw, session, **kwargs)

    monkeypatch.setattr(ingest_module, "ingest_one_row", _flaky_ingest_one_row)

    resp = await client.post(
        "/api/ingest/records/batch",
        headers=otto_ingest_headers,
        json={
            "tenant": "otto",
            "vertical": "auto",
            "records": [_record(good1), _record(bad), _record(good2)],
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["status"] for r in results] == ["created", "error", "created"]
    assert "simulated statement failure" in results[1]["errors"][0]

    async with async_session_factory() as s:
        counts = (
            await s.execute(
                text(
                    "SELECT"
                    " (SELECT count(*) FROM recipients WHERE tenant='otto' AND email=:g1) AS g1,"
                    " (SELECT count(*) FROM recipients WHERE tenant='otto' AND email=:bad) AS bad,"
                    " (SELECT count(*) FROM recipients WHERE tenant='otto' AND email=:g2) AS g2"
                ),
                {"g1": good1, "bad": bad, "g2": good2},
            )
        ).mappings().one()
    assert (counts["g1"], counts["bad"], counts["g2"]) == (1, 0, 1)


@pytest.mark.asyncio
async def test_batch_size_is_capped(client, otto_ingest_headers):
    resp = await client.post(
        "/api/ingest/records/batch",
        headers=otto_ingest_headers,
        json={
            "tenant": "otto",
            "vertical": "auto",
            "records": [_record(f"cap-{n}@cadencetests.io") for n in range(201)],
        },
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("key", ["wrong-key", "test-api-key", ""])
async def test_batch_rejects_non_ingestion_keys(client, key: str) -> None:
    response = await client.post(
        "/api/ingest/records/batch",
        headers={"X-API-Key": key},
        json={"tenant": "otto", "vertical": "auto", "records": []},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("tenant", ["other", "OTTO", "otto ", ""])
async def test_otto_key_cannot_ingest_another_tenant(client, otto_ingest_headers, tenant: str) -> None:
    email = f"api-scope-{uuid.uuid4()}@cadencetests.io"
    response = await client.post(
        "/api/ingest/records/batch",
        headers=otto_ingest_headers,
        json={"tenant": tenant, "vertical": "auto", "records": [_record(email)]},
    )
    assert response.status_code == 403
    async with async_session_factory() as session:
        count = await session.scalar(
            text("SELECT count(*) FROM recipients WHERE email=:email"), {"email": email}
        )
    assert count == 0


async def test_otto_key_defaults_to_otto_tenant(client, otto_ingest_headers) -> None:
    email = f"api-default-{uuid.uuid4()}@cadencetests.io"
    response = await client.post(
        "/api/ingest/records/batch",
        headers=otto_ingest_headers,
        json={"vertical": "auto", "records": [_record(email)]},
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "created"
    async with async_session_factory() as session:
        tenant = await session.scalar(
            text("SELECT tenant FROM recipients WHERE email=:email"), {"email": email}
        )
    assert tenant == "otto"


@pytest.mark.parametrize("key", [None, ""])
async def test_batch_fails_closed_without_configured_otto_key(
    client, monkeypatch, key: str | None
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.core.auth.get_settings",
        lambda: SimpleNamespace(api_key="test-api-key", otto_ingest_api_key=key),
    )
    response = await client.post(
        "/api/ingest/records/batch",
        headers={"X-API-Key": ""},
        json={"tenant": "otto", "vertical": "auto", "records": []},
    )
    assert response.status_code == 401


async def test_otto_key_cannot_access_other_api_routes(client, otto_ingest_headers) -> None:
    response = await client.get("/api/suppressions", headers=otto_ingest_headers)
    assert response.status_code == 401
    response = await client.post(
        "/api/ingest/recipients",
        headers=otto_ingest_headers,
        json={"tenant": "otto", "vertical": "auto", "email": "scope@cadencetests.io"},
    )
    assert response.status_code == 401
