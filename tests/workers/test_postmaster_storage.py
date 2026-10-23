"""postmaster_daily storage and history-endpoint tests."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.adapters.postmaster import DailyMetric
from app.workers import postmaster_sync


@pytest.mark.asyncio
async def test_store_writes_rows(db_session, monkeypatch):
    class _S:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(postmaster_sync, "async_session_factory", lambda: _S())

    n = await postmaster_sync._store(
        [
            DailyMetric(day=date(2026, 10, 12), metric="SPAM_RATE", value=0.0),
            DailyMetric(day=date(2026, 10, 19), metric="SPAM_RATE", value=0.004),
        ],
        "domains/karzinsurance.com",
    )
    assert n == 2

    rows = (await db_session.execute(text(
        "SELECT day, metric, value FROM postmaster_daily ORDER BY day"
    ))).all()
    assert [(r[1], float(r[2])) for r in rows] == [
        ("SPAM_RATE", 0.0), ("SPAM_RATE", 0.004),
    ]


@pytest.mark.asyncio
async def test_store_upserts_rather_than_duplicating(db_session, monkeypatch):
    """Postmaster restates recent days; the lookback window re-reads them."""
    class _S:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(postmaster_sync, "async_session_factory", lambda: _S())

    row = [DailyMetric(day=date(2026, 10, 19), metric="SPAM_RATE", value=0.001)]
    await postmaster_sync._store(row, "domains/karzinsurance.com")
    # Same day, restated higher.
    await postmaster_sync._store(
        [DailyMetric(day=date(2026, 10, 19), metric="SPAM_RATE", value=0.005)],
        "domains/karzinsurance.com",
    )

    rows = (await db_session.execute(text(
        "SELECT value FROM postmaster_daily WHERE day = '2026-10-19'"
        " AND metric = 'SPAM_RATE'"
    ))).all()
    assert len(rows) == 1, "a restated day must overwrite, not accumulate"
    assert float(rows[0][0]) == 0.005


@pytest.mark.asyncio
async def test_store_is_noop_for_empty_input(db_session, monkeypatch):
    class _S:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(postmaster_sync, "async_session_factory", lambda: _S())
    assert await postmaster_sync._store([], "domains/x.com") == 0


@pytest.mark.asyncio
async def test_healthy_reading_is_persisted(db_session, monkeypatch):
    """The gap this closes: a below-threshold day wrote nothing at all, so
    there was no baseline to trend against."""
    from app.adapters.postmaster import MockPostmasterClient

    today = date.today()

    class _S:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *a):
            return False

    class _Settings:
        postmaster_domain = "domains/karzinsurance.com"
        postmaster_lookback_days = 7
        postmaster_spam_rate_warn = 0.001
        postmaster_spam_rate_critical = 0.003
        alert_tenants = ["otto"]
        alert_backend = "noop"
        alert_sustained_minutes = 0

    monkeypatch.setattr(
        postmaster_sync, "get_postmaster_client",
        lambda: MockPostmasterClient({today: 0.0}),  # perfectly healthy
    )
    monkeypatch.setattr(postmaster_sync, "get_settings", lambda: _Settings())
    monkeypatch.setattr(postmaster_sync, "async_session_factory", lambda: _S())

    result = await postmaster_sync.run_once()
    assert result["stored"] > 0

    stored = (await db_session.execute(text(
        "SELECT count(*) FROM postmaster_daily WHERE metric = 'SPAM_RATE'"
    ))).scalar()
    assert stored == 1, "a healthy day must still be recorded"

    # And no alert, because it is below warn.
    alerts = (await db_session.execute(text(
        "SELECT count(*) FROM alerts WHERE metric = 'gmail_spam_rate'"
    ))).scalar()
    assert alerts == 0


@pytest.mark.asyncio
async def test_one_failing_metric_does_not_lose_the_others(db_session, monkeypatch):
    """An unsupported metric 400s; the spam rate must still land."""
    from app.adapters.postmaster import PostmasterError

    today = date.today()

    class Partial:
        async def domains(self):
            return ["domains/x.com"]

        async def daily_metric(self, domain, metric, start, end):
            if metric == "DELIVERY_ERROR_RATE":
                raise PostmasterError("POST ...: 400 INVALID_ARGUMENT")
            return [DailyMetric(day=today, metric=metric, value=0.0)]

    class _S:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *a):
            return False

    class _Settings:
        postmaster_domain = "domains/x.com"
        postmaster_lookback_days = 7
        postmaster_spam_rate_warn = 0.001
        postmaster_spam_rate_critical = 0.003
        alert_tenants = ["otto"]
        alert_backend = "noop"
        alert_sustained_minutes = 0

    monkeypatch.setattr(postmaster_sync, "get_postmaster_client", lambda: Partial())
    monkeypatch.setattr(postmaster_sync, "get_settings", lambda: _Settings())
    monkeypatch.setattr(postmaster_sync, "async_session_factory", lambda: _S())

    result = await postmaster_sync.run_once()
    assert result["checked"] == 1, "spam rate must still be evaluated"

    metrics = {r[0] for r in (await db_session.execute(text(
        "SELECT DISTINCT metric FROM postmaster_daily"
    ))).all()}
    assert "SPAM_RATE" in metrics
    assert "DELIVERY_ERROR_RATE" not in metrics


@pytest.mark.asyncio
async def test_history_endpoint_returns_series(client, db_session):
    await db_session.execute(text(
        "INSERT INTO postmaster_daily (domain, day, metric, value) VALUES"
        " ('domains/karzinsurance.com', :d1, 'SPAM_RATE', 0.0),"
        " ('domains/karzinsurance.com', :d2, 'SPAM_RATE', 0.004)"
    ), {"d1": date.today() - timedelta(days=2), "d2": date.today()})
    await db_session.commit()

    resp = await client.get(
        "/api/postmaster/history",
        params={"metric": "SPAM_RATE", "days": 30,
                "domain": "karzinsurance.com"},
        headers={"X-API-Key": "test-api-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["domain"] == "domains/karzinsurance.com"
    assert [p["value"] for p in body["points"]] == [0.0, 0.004]
    # Percent conversion guards against misreading a ratio as a percentage.
    assert body["points"][1]["value_pct"] == pytest.approx(0.4)
    assert body["warn_threshold"] == 0.001
    assert body["critical_threshold"] == 0.003


@pytest.mark.asyncio
async def test_history_endpoint_requires_api_key(client):
    resp = await client.get("/api/postmaster/history")
    assert resp.status_code in (401, 403)
