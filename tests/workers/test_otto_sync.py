"""Tests for the OTTO unsub_outbox sync worker."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.adapters.otto import client as otto_client_module
from app.models.unsub_outbox import UnsubOutbox
from app.workers import otto_sync


def _email() -> str:
    return f"test-{uuid.uuid4()}@cadencetests.io"


async def _seed_pending_row(db_session, email: str) -> uuid.UUID:
    row = UnsubOutbox(
        tenant="otto",
        email=email,
        reason="unsubscribe",
        occurred_at=datetime.now(timezone.utc),
        attempts=0,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row.id


async def test_run_once_delivers_when_endpoint_unset(db_session):
    """With `otto_unsub_endpoint=None` the client's simulated path succeeds."""
    email = _email()
    row_id = await _seed_pending_row(db_session, email)

    result = await otto_sync.run_once()

    assert result == {"delivered": 1, "failed": 0, "processed": 1}

    db_session.expire_all()
    refreshed = await db_session.get(UnsubOutbox, row_id)
    assert refreshed is not None
    assert refreshed.delivered_at is not None
    assert refreshed.attempts == 1
    assert refreshed.last_attempt_at is not None
    assert refreshed.last_error is None


async def test_run_once_records_failure_on_push_error(db_session, monkeypatch):
    """Simulate OttoClient.push_unsubscribe raising → row stays undelivered."""
    email = _email()
    row_id = await _seed_pending_row(db_session, email)

    async def _raise(self, email, reason, occurred_at):
        raise otto_client_module.OttoPushError("boom")

    monkeypatch.setattr(
        otto_client_module.OttoClient, "push_unsubscribe", _raise
    )

    result = await otto_sync.run_once()

    assert result == {"delivered": 0, "failed": 1, "processed": 1}

    db_session.expire_all()
    refreshed = await db_session.get(UnsubOutbox, row_id)
    assert refreshed is not None
    assert refreshed.delivered_at is None
    assert refreshed.attempts == 1
    assert refreshed.last_attempt_at is not None
    assert refreshed.last_error is not None
    assert "boom" in refreshed.last_error
