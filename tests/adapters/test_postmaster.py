"""Postmaster v2 client tests.

Fixtures mirror responses actually observed against
domains/karzinsurance.com, including the failure modes: v1's retirement 429,
the per-metric 400, and volume-gated empty results.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.adapters.postmaster import (
    DailyMetric,
    GooglePostmasterClient,
    MockPostmasterClient,
    PostmasterError,
    _statistic_value,
)


@pytest.fixture
def key_file(tmp_path):
    """A real RSA service-account key, so JWT signing is genuinely exercised."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({
        "type": "service_account",
        "project_id": "test-project",
        "client_email": "postmaster-reader@test-project.iam.gserviceaccount.com",
        "private_key": pem,
        "token_uri": "https://oauth2.googleapis.com/token",
    }), encoding="utf-8")
    return path


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.fixture
def patched_httpx(monkeypatch):
    """Route every AsyncClient through a caller-supplied handler."""
    def install(handler):
        original = httpx.AsyncClient

        class Patched(original):
            def __init__(self, *a, **kw):
                kw["transport"] = _transport(handler)
                super().__init__(*a, **kw)

        monkeypatch.setattr(httpx, "AsyncClient", Patched)
    return install


def _token_response():
    return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})


@pytest.mark.asyncio
async def test_domains_lists_registered_domains(key_file, patched_httpx):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return _token_response()
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"domains": [{
            "name": "domains/karzinsurance.com",
            "permission": "READER",
            "verificationState": "VERIFIED",
        }]})

    patched_httpx(handler)
    client = GooglePostmasterClient(key_file)
    assert await client.domains() == ["domains/karzinsurance.com"]
    assert seen["auth"] == "Bearer tok-123"
    assert seen["url"].endswith("/v2/domains")


@pytest.mark.asyncio
async def test_daily_metric_parses_rows_and_sorts_by_day(key_file, patched_httpx):
    body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return _token_response()
        body.update(json.loads(request.content))
        return httpx.Response(200, json={"domainStats": [
            {"date": {"year": 2026, "month": 10, "day": 19},
             "metric": "SPAM_RATE", "value": {"doubleValue": 0.004}},
            {"date": {"year": 2026, "month": 10, "day": 12},
             "metric": "SPAM_RATE", "value": {"doubleValue": 0.0}},
        ]})

    patched_httpx(handler)
    client = GooglePostmasterClient(key_file)
    rows = await client.daily_metric(
        "karzinsurance.com", "SPAM_RATE", date(2026, 10, 12), date(2026, 10, 19)
    )

    assert rows == [
        DailyMetric(day=date(2026, 10, 12), metric="SPAM_RATE", value=0.0),
        DailyMetric(day=date(2026, 10, 19), metric="SPAM_RATE", value=0.004),
    ]
    # Exactly one metric per request: batching 400s the whole call.
    assert len(body["metricDefinitions"]) == 1
    assert body["aggregationGranularity"] == "DAILY"


@pytest.mark.asyncio
async def test_bare_domain_is_prefixed(key_file, patched_httpx):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return _token_response()
        seen["path"] = request.url.path
        return httpx.Response(200, json={"domainStats": []})

    patched_httpx(handler)
    await GooglePostmasterClient(key_file).daily_metric(
        "karzinsurance.com", "SPAM_RATE", date(2026, 10, 1), date(2026, 10, 2)
    )
    assert seen["path"] == "/v2/domains/karzinsurance.com/domainStats:query"


@pytest.mark.asyncio
async def test_volume_gated_empty_result_is_not_an_error(key_file, patched_httpx):
    """Postmaster returns no rows below its reporting threshold."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return _token_response()
        return httpx.Response(200, json={})

    patched_httpx(handler)
    rows = await GooglePostmasterClient(key_file).daily_metric(
        "karzinsurance.com", "FEEDBACK_LOOP_ID", date(2026, 10, 1), date(2026, 10, 2)
    )
    assert rows == []


@pytest.mark.asyncio
async def test_unsupported_metric_400_raises(key_file, patched_httpx):
    """AUTH_SUCCESS_RATE 400s on a low-volume domain; surface it, don't swallow."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return _token_response()
        return httpx.Response(400, json={"error": {
            "code": 400, "message": "Request contains an invalid argument.",
            "status": "INVALID_ARGUMENT"}})

    patched_httpx(handler)
    with pytest.raises(PostmasterError, match="400"):
        await GooglePostmasterClient(key_file).daily_metric(
            "karzinsurance.com", "AUTH_SUCCESS_RATE",
            date(2026, 10, 1), date(2026, 10, 2),
        )


@pytest.mark.asyncio
async def test_retired_v1_429_raises(key_file, patched_httpx):
    """Guards the version regression: v1 answers every call with 429."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return _token_response()
        return httpx.Response(429, json={"error": {
            "code": 429,
            "message": "This version of the Postmaster Tools API is no longer "
                       "supported.",
            "status": "RESOURCE_EXHAUSTED"}})

    patched_httpx(handler)
    with pytest.raises(PostmasterError, match="429"):
        await GooglePostmasterClient(key_file).domains()


@pytest.mark.asyncio
async def test_token_is_reused_across_calls(key_file, patched_httpx):
    calls = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            calls["token"] += 1
            return _token_response()
        return httpx.Response(200, json={"domains": []})

    patched_httpx(handler)
    client = GooglePostmasterClient(key_file)
    await client.domains()
    await client.domains()
    assert calls["token"] == 1


@pytest.mark.asyncio
async def test_token_failure_is_reported(key_file, patched_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid_grant")

    patched_httpx(handler)
    with pytest.raises(PostmasterError, match="token exchange failed"):
        await GooglePostmasterClient(key_file).domains()


@pytest.mark.asyncio
async def test_missing_key_file_is_reported(tmp_path):
    client = GooglePostmasterClient(tmp_path / "nope.json")
    with pytest.raises(PostmasterError, match="cannot read service-account key"):
        await client.domains()


@pytest.mark.asyncio
async def test_incomplete_credentials_are_reported(tmp_path):
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({"client_email": "x@y.z"}), encoding="utf-8")
    with pytest.raises(PostmasterError, match="missing 'private_key'"):
        await GooglePostmasterClient(path).domains()


def test_statistic_value_flattens_union():
    assert _statistic_value({"doubleValue": 0.25}) == 0.25
    assert _statistic_value({"intValue": "42"}) == 42.0
    assert _statistic_value({"stringValue": "abc"}) is None
    assert _statistic_value({}) is None


@pytest.mark.asyncio
async def test_mock_client_filters_to_range():
    mock = MockPostmasterClient({
        date(2026, 10, 1): 0.0,
        date(2026, 10, 5): 0.002,
        date(2026, 10, 9): 0.01,
    })
    rows = await mock.daily_metric(
        "d", "SPAM_RATE", date(2026, 10, 2), date(2026, 10, 6)
    )
    assert [r.day for r in rows] == [date(2026, 10, 5)]
