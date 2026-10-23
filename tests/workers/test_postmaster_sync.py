"""postmaster_sync worker tests."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.adapters.postmaster import MockPostmasterClient, PostmasterError
from app.workers import postmaster_sync


class _Boom:
    async def domains(self):
        raise PostmasterError("boom")

    async def daily_metric(self, *a, **kw):
        raise PostmasterError("boom")


@pytest.mark.asyncio
async def test_noop_when_no_client(monkeypatch):
    monkeypatch.setattr(postmaster_sync, "get_postmaster_client", lambda: None)
    assert await postmaster_sync.run_once() == {"checked": 0, "days": 0, "stored": 0}


@pytest.mark.asyncio
async def test_api_failure_does_not_raise(monkeypatch):
    """A Postmaster outage must not take the scheduler down."""
    monkeypatch.setattr(postmaster_sync, "get_postmaster_client", lambda: _Boom())
    monkeypatch.setattr(
        postmaster_sync, "get_settings",
        lambda: _settings(domain="domains/x.com"),
    )
    assert await postmaster_sync.run_once() == {"checked": 0, "days": 0, "stored": 0}


def _settings(**over):
    from app.core.config import get_settings
    s = get_settings()

    class S:
        postmaster_domain = over.get("domain")
        postmaster_lookback_days = over.get("lookback", 7)
        postmaster_spam_rate_warn = over.get("warn", 0.001)
        postmaster_spam_rate_critical = over.get("critical", 0.003)
        postmaster_sync_interval_seconds = 21600
        alert_tenants = over.get("tenants", ["otto"])
        alert_sustained_minutes = s.alert_sustained_minutes
        alert_backend = "noop"

    return S()


@pytest.mark.asyncio
async def test_ambiguous_domain_is_refused(monkeypatch):
    """Two registered domains and no explicit setting: alert on neither."""
    class Two:
        async def domains(self):
            return ["domains/a.com", "domains/b.com"]

        async def daily_metric(self, *a, **kw):  # pragma: no cover
            raise AssertionError("must not query an arbitrary domain")

    monkeypatch.setattr(postmaster_sync, "get_postmaster_client", lambda: Two())
    monkeypatch.setattr(postmaster_sync, "get_settings", lambda: _settings())
    assert await postmaster_sync.run_once() == {"checked": 0, "days": 0, "stored": 0}


@pytest.mark.asyncio
async def test_no_data_leaves_alert_state_untouched(monkeypatch):
    """Volume-gated absence must not look like a healthy zero."""
    calls = []

    async def spy(*a, **kw):
        calls.append(a)

    monkeypatch.setattr(
        postmaster_sync, "get_postmaster_client",
        lambda: MockPostmasterClient({}),
    )
    monkeypatch.setattr(
        postmaster_sync, "get_settings",
        lambda: _settings(domain="domains/x.com"),
    )
    monkeypatch.setattr(postmaster_sync, "_apply_alert_state", spy)

    assert await postmaster_sync.run_once() == {"checked": 0, "days": 0, "stored": 0}
    assert calls == [], "alert state must not be evaluated without data"


@pytest.mark.asyncio
async def test_latest_day_is_evaluated(monkeypatch):
    """The newest day drives the alert, not the first or an average."""
    captured = {}

    async def spy(db, tenant, metric, value, warn, crit, dispatcher, now):
        captured.update(
            tenant=tenant, metric=metric, value=value, warn=warn, crit=crit
        )

    today = date.today()
    monkeypatch.setattr(
        postmaster_sync, "get_postmaster_client",
        lambda: MockPostmasterClient({
            today - timedelta(days=3): 0.0,
            today - timedelta(days=1): 0.0042,
        }),
    )
    monkeypatch.setattr(
        postmaster_sync, "get_settings",
        lambda: _settings(domain="domains/karzinsurance.com"),
    )
    monkeypatch.setattr(postmaster_sync, "_apply_alert_state", spy)

    result = await postmaster_sync.run_once()
    assert result["checked"] == 1
    assert result["days"] == 2
    assert captured["value"] == 0.0042
    assert captured["metric"] == "gmail_spam_rate"
    assert captured["tenant"] == "otto"
    assert captured["warn"] == 0.001
    assert captured["crit"] == 0.003


@pytest.mark.asyncio
async def test_metric_name_does_not_collide_with_ses_complaint_rate():
    """Gmail spam rate and SES complaint rate are different signals."""
    assert postmaster_sync.METRIC == "gmail_spam_rate"
    assert postmaster_sync.METRIC != "complaint_rate"


@pytest.mark.asyncio
async def test_alert_row_is_written_end_to_end(db_session, monkeypatch):
    """Exercises the real _apply_alert_state against the DB."""
    today = date.today()
    monkeypatch.setattr(
        postmaster_sync, "get_postmaster_client",
        lambda: MockPostmasterClient({today: 0.01}),  # 1%, well over critical
    )
    monkeypatch.setattr(
        postmaster_sync, "get_settings",
        lambda: _settings(domain="domains/karzinsurance.com"),
    )

    class _Session:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(postmaster_sync, "async_session_factory", lambda: _Session())

    result = await postmaster_sync.run_once()
    assert result["checked"] == 1

    rows = (await db_session.execute(text(
        "SELECT metric, severity, value FROM alerts WHERE metric = :m"
    ), {"m": "gmail_spam_rate"})).mappings().all()
    assert len(rows) == 1
    assert rows[0]["value"] == pytest.approx(0.01)
