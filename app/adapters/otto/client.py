from __future__ import annotations

from datetime import datetime

import httpx
import structlog

from app.core.config import get_settings


class OttoPushError(Exception):
    pass


class OttoClient:
    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._log = structlog.get_logger(__name__)

    async def push_unsubscribe(
        self, email: str, reason: str, occurred_at: datetime
    ) -> None:
        settings = get_settings()
        endpoint = settings.otto_unsub_endpoint

        if endpoint is None:
            self._log.info(
                "simulated OTTO unsub push",
                email=email,
                reason=reason,
                occurred_at=occurred_at.isoformat(),
            )
            return

        headers: dict[str, str] = {}
        if settings.otto_api_key:
            headers["Authorization"] = f"Bearer {settings.otto_api_key}"

        payload = {
            "email": email,
            "reason": reason,
            "occurred_at": occurred_at.isoformat(),
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(endpoint, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise OttoPushError(f"transport error: {exc}") from exc

        if response.status_code >= 400:
            raise OttoPushError(
                f"OTTO unsub push failed: {response.status_code} {response.text[:200]}"
            )
