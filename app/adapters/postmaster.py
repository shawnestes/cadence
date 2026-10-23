"""Google Postmaster Tools API v2 client.

Why hand-rolled: the service-account OAuth flow is a signed JWT exchanged for
an access token, which `cryptography` (already a dependency for unsub token
signing) and `httpx` cover between them. Pulling in `google-auth` for ~40
lines of RS256 would add a dependency tree for no capability.

Version matters. The v1 API that `trafficStats` belonged to is retired and now
answers every call with HTTP 429 RESOURCE_EXHAUSTED ("This version of the
Postmaster Tools API is no longer supported"). v2 replaced it with a different
shape and different scopes:

    v1  GET  /v1/domains/{d}/trafficStats     scope postmaster.readonly
    v2  POST /v2/domains/{d}/domainStats:query scope postmaster.traffic.readonly

One metric per request. A single unsupported metric fails the *whole* batch
with 400 INVALID_ARGUMENT, and which metrics are supported varies by domain and
by volume -- on a low-volume domain SPAM_RATE and DELIVERY_ERROR_* answer while
AUTH_SUCCESS_RATE and TLS_ENCRYPTION_* return 400. Batching them would mean one
unsupported metric blinds us to the spam rate, which is the number that matters.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import httpx
import structlog
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.core.config import get_settings

log = structlog.get_logger(__name__)

_BASE = "https://gmailpostmastertools.googleapis.com/v2"
# Two scopes, because the two calls differ: domainStats:query accepts
# postmaster.traffic.readonly, but GET /v2/domains rejects it with
# 403 PERMISSION_DENIED ("insufficient authentication scopes") and requires
# postmaster.domain. Requesting only the traffic scope breaks domain discovery.
_SCOPES = " ".join((
    "https://www.googleapis.com/auth/postmaster.traffic.readonly",
    "https://www.googleapis.com/auth/postmaster.domain",
))
_TOKEN_LIFETIME_S = 3600
# Refresh a little early; a token that expires mid-flight reads as a 401.
_TOKEN_SKEW_S = 300

# Gmail publishes a 0.3% hard ceiling and recommends staying under 0.1%.
# The API reports a ratio, not a percentage, so 0.001 == 0.1%.
SPAM_RATE_METRIC = "SPAM_RATE"


class PostmasterError(Exception):
    """The Postmaster API call failed."""


@dataclass(frozen=True)
class DailyMetric:
    day: date
    metric: str
    value: float


class PostmasterClient(Protocol):
    async def domains(self) -> list[str]: ...

    async def daily_metric(
        self, domain: str, metric: str, start: date, end: date
    ) -> list[DailyMetric]: ...


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


class GooglePostmasterClient:
    """Reads Postmaster v2 with a service-account JWT.

    The service account must be granted READER on the domain inside Postmaster
    Tools -- the API only returns domains registered to the calling identity, so
    a project-level grant alone yields an empty `domains` list.
    """

    def __init__(self, credentials_path: str | Path) -> None:
        self._path = Path(credentials_path)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _credentials(self) -> dict:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PostmasterError(
                f"cannot read service-account key at {self._path}: {exc}"
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PostmasterError(f"{self._path} is not valid JSON: {exc}") from exc
        for required in ("client_email", "private_key", "token_uri"):
            if not data.get(required):
                raise PostmasterError(f"{self._path} is missing {required!r}")
        return data

    def _signed_assertion(self, creds: dict, now: int) -> str:
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": creds["client_email"],
            "scope": _SCOPES,
            "aud": creds["token_uri"],
            "iat": now,
            "exp": now + _TOKEN_LIFETIME_S,
        }
        signing_input = b".".join((
            _b64(json.dumps(header, separators=(",", ":")).encode()),
            _b64(json.dumps(claims, separators=(",", ":")).encode()),
        ))
        key = serialization.load_pem_private_key(
            creds["private_key"].encode(), password=None
        )
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return (signing_input + b"." + _b64(signature)).decode()

    async def _access_token(self) -> str:
        now = int(time.time())
        if self._token and now < self._token_expires_at - _TOKEN_SKEW_S:
            return self._token

        creds = self._credentials()
        assertion = self._signed_assertion(creds, now)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                creds["token_uri"],
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        if resp.status_code != 200:
            raise PostmasterError(
                f"token exchange failed: {resp.status_code} {resp.text[:200]}"
            )
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise PostmasterError("token exchange returned no access_token")
        self._token = token
        self._token_expires_at = now + int(payload.get("expires_in", _TOKEN_LIFETIME_S))
        return token

    async def _get(self, path: str) -> dict:
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.get(
                f"{_BASE}/{path}", headers={"Authorization": f"Bearer {token}"}
            )
        if resp.status_code != 200:
            raise PostmasterError(f"GET {path}: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def _post(self, path: str, body: dict) -> dict:
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.post(
                f"{_BASE}/{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
        if resp.status_code != 200:
            raise PostmasterError(f"POST {path}: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def domains(self) -> list[str]:
        """Registered domain resource names, e.g. `domains/karzinsurance.com`."""
        payload = await self._get("domains")
        return [d["name"] for d in payload.get("domains", []) if d.get("name")]

    async def daily_metric(
        self, domain: str, metric: str, start: date, end: date
    ) -> list[DailyMetric]:
        """Daily values for one metric. Missing days simply do not come back --
        Postmaster only reports days with enough traffic to measure, so an empty
        list means "no data at this volume", not zero."""
        name = domain if domain.startswith("domains/") else f"domains/{domain}"
        body = {
            "timeQuery": {
                "dateRanges": {
                    "dateRanges": [{
                        "start": {"year": start.year, "month": start.month,
                                  "day": start.day},
                        "end": {"year": end.year, "month": end.month, "day": end.day},
                    }]
                }
            },
            "aggregationGranularity": "DAILY",
            "metricDefinitions": [{"baseMetric": {"standardMetric": metric}}],
        }
        payload = await self._post(f"{name}/domainStats:query", body)

        out: list[DailyMetric] = []
        for row in payload.get("domainStats", []):
            d = row.get("date") or {}
            if not all(d.get(k) for k in ("year", "month", "day")):
                continue
            value = _statistic_value(row.get("value") or {})
            if value is None:
                continue
            out.append(DailyMetric(
                day=date(d["year"], d["month"], d["day"]),
                metric=row.get("metric") or metric,
                value=value,
            ))
        return sorted(out, key=lambda m: m.day)


def _statistic_value(value: dict) -> float | None:
    """Flatten a StatisticValue union to a float, or None when non-numeric."""
    for key in ("doubleValue", "floatValue"):
        if key in value:
            return float(value[key])
    if "intValue" in value:
        # intValue is int64-as-string per the discovery doc.
        try:
            return float(value["intValue"])
        except (TypeError, ValueError):
            return None
    return None


class MockPostmasterClient:
    """Deterministic stand-in for tests and for dev without a key."""

    def __init__(self, values: dict[date, float] | None = None) -> None:
        self._values = values or {}

    async def domains(self) -> list[str]:
        return ["domains/example.com"]

    async def daily_metric(
        self, domain: str, metric: str, start: date, end: date
    ) -> list[DailyMetric]:
        day = start
        out: list[DailyMetric] = []
        while day <= end:
            if day in self._values:
                out.append(DailyMetric(day=day, metric=metric,
                                       value=self._values[day]))
            day += timedelta(days=1)
        return out


def get_postmaster_client() -> PostmasterClient | None:
    """The configured client, or None when Postmaster is not set up.

    Returning None rather than raising lets the worker register
    unconditionally and no-op until credentials exist, the same way the SQS
    poller handles a missing queue URL.
    """
    settings = get_settings()
    if settings.postmaster_backend == "mock":
        return MockPostmasterClient()
    if not settings.postmaster_credentials_path:
        return None
    return GooglePostmasterClient(settings.postmaster_credentials_path)
