"""POST /api/ingest/records/batch."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.db import async_session_factory

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
async def test_batch_reports_per_row_status(client, auth_headers):
    good = f"api-good-{uuid.uuid4()}@cadencetests.io"
    noconsent = f"api-nc-{uuid.uuid4()}@cadencetests.io"
    resp = await client.post(
        "/api/ingest/records/batch",
        headers=auth_headers,
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
async def test_batch_survives_a_bad_row_at_the_database_level(client, auth_headers, monkeypatch):
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
        headers=auth_headers,
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
async def test_batch_size_is_capped(client, auth_headers):
    resp = await client.post(
        "/api/ingest/records/batch",
        headers=auth_headers,
        json={
            "tenant": "otto",
            "vertical": "auto",
            "records": [_record(f"cap-{n}@cadencetests.io") for n in range(201)],
        },
    )
    assert resp.status_code == 422
