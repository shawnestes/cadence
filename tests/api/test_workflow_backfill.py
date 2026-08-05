"""Tests for POST /api/workflows/{id}/backfill."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.core.db import async_session_factory
from app.models.journey import Journey
from tests.api._contact_profile_seed import seed_contact_profile


def _email() -> str:
    return f"test-{uuid.uuid4()}@cadencetests.io"


def _recipient_payload(email: str, **overrides) -> dict:
    body = {
        "email": email,
        "external_id": "otto_test",
        "vertical": "auto",
        "consent_source": "otto_form",
        "consent_at": "2026-04-21T14:23:00Z",
        "attributes": {"first_name": "Sarah", "state": "TX"},
    }
    body.update(overrides)
    return body


def _definition(exit_reason: str = "completed") -> dict:
    return {
        "version": 1,
        "name": "test-wf",
        "vertical": "auto",
        "entry": "entry",
        "nodes": {
            "entry": {"type": "entry", "next": "exit_done"},
            "exit_done": {"type": "exit", "reason": exit_reason},
        },
    }


def _workflow_body(*, reentry_cooldown: str | None = None, **overrides) -> dict:
    body = {
        "name": f"test-wf-{uuid.uuid4()}",
        "tenant": "otto",
        "vertical": "auto",
        "definition": _definition(),
        # Non-matching entry_segment so ingest-time auto-enroll never fires;
        # backfill's segment is independent of this.
        "entry_segment": {"vertical": "home"},
        "reentry_cooldown": reentry_cooldown,
    }
    body.update(overrides)
    return body


async def _create_and_activate_workflow(client, auth_headers, **overrides) -> str:
    create = await client.post(
        "/api/workflows", json=_workflow_body(**overrides), headers=auth_headers
    )
    assert create.status_code == 201, create.text
    wid = create.json()["id"]
    resp = await client.post(f"/api/workflows/{wid}/activate", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    return wid


async def _ingest(client, auth_headers, **overrides) -> str:
    resp = await client.post(
        "/api/ingest/recipients",
        json=_recipient_payload(_email(), **overrides),
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _ingest_with_profile(
    client, auth_headers, *, email_domain: str, **overrides
) -> str:
    """Ingest a recipient and seed a matching contact_profile row.

    compile_segment targets contact_profile, not recipients.attributes, so
    backfill tests select candidates by email_domain rather than the
    ingest-time attributes bag.
    """
    rid = await _ingest(client, auth_headers, **overrides)
    await seed_contact_profile(rid, email_domain=email_domain)
    return rid


async def _journeys_for_workflow(wid: str) -> list[Journey]:
    async with async_session_factory() as s:
        return (
            (await s.execute(select(Journey).where(Journey.workflow_id == uuid.UUID(wid))))
            .scalars()
            .all()
        )


async def _mark_terminal(journey_id, *, status: str, completed_at: datetime) -> None:
    async with async_session_factory() as s:
        await s.execute(
            text(
                "UPDATE journeys SET status = :status, completed_at = :completed_at, "
                "next_action_at = NULL WHERE id = :id"
            ),
            {"status": status, "completed_at": completed_at, "id": journey_id},
        )
        await s.commit()


async def test_backfill_enrolls_matching_recipients(client, auth_headers):
    wid = await _create_and_activate_workflow(client, auth_headers)
    r1 = await _ingest_with_profile(client, auth_headers, email_domain="tx.cadencetests.io")
    r2 = await _ingest_with_profile(client, auth_headers, email_domain="tx.cadencetests.io")
    await _ingest_with_profile(client, auth_headers, email_domain="ca.cadencetests.io")

    resp = await client.post(
        f"/api/workflows/{wid}/backfill",
        json={"segment": {"email_domain": "tx.cadencetests.io"}, "limit": 100},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["enrolled"] == 2
    assert data["skipped_active"] == 0
    assert data["skipped_cooldown"] == 0
    assert data["skipped_suppressed"] == 0

    journeys = await _journeys_for_workflow(wid)
    enrolled_recipient_ids = {str(j.recipient_id) for j in journeys}
    assert enrolled_recipient_ids == {r1, r2}


async def test_backfill_respects_limit(client, auth_headers):
    wid = await _create_and_activate_workflow(client, auth_headers)
    for _ in range(3):
        await _ingest_with_profile(client, auth_headers, email_domain="tx.cadencetests.io")

    resp = await client.post(
        f"/api/workflows/{wid}/backfill",
        json={"segment": {"email_domain": "tx.cadencetests.io"}, "limit": 2},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enrolled"] == 2


async def test_backfill_limit_over_10000_returns_422(client, auth_headers):
    wid = await _create_and_activate_workflow(client, auth_headers)
    resp = await client.post(
        f"/api/workflows/{wid}/backfill",
        json={"segment": {}, "limit": 10001},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_backfill_skips_suppressed_recipients(client, auth_headers):
    wid = await _create_and_activate_workflow(client, auth_headers)
    email = _email()
    resp = await client.post(
        "/api/ingest/recipients",
        json=_recipient_payload(email, attributes={"state": "TX"}),
        headers=auth_headers,
    )
    assert resp.status_code == 201
    await seed_contact_profile(resp.json()["id"], email_domain="tx.cadencetests.io")

    sup = await client.post(
        "/api/suppressions",
        json={"tenant": "otto", "email": email, "reason": "complaint", "source": "test"},
        headers=auth_headers,
    )
    assert sup.status_code in (200, 201), sup.text

    resp = await client.post(
        f"/api/workflows/{wid}/backfill",
        json={"segment": {"email_domain": "tx.cadencetests.io"}, "limit": 100},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["skipped_suppressed"] == 1
    assert data["enrolled"] == 0


async def test_backfill_skips_active_journey(client, auth_headers):
    wid = await _create_and_activate_workflow(client, auth_headers)
    rid = await _ingest_with_profile(client, auth_headers, email_domain="tx.cadencetests.io")

    # First backfill enrolls; second (before the journey completes) hits the
    # active-journey uniqueness guard.
    first = await client.post(
        f"/api/workflows/{wid}/backfill",
        json={"segment": {"email_domain": "tx.cadencetests.io"}, "limit": 100},
        headers=auth_headers,
    )
    assert first.status_code == 200
    assert first.json()["enrolled"] == 1

    second = await client.post(
        f"/api/workflows/{wid}/backfill",
        json={"segment": {"email_domain": "tx.cadencetests.io"}, "limit": 100},
        headers=auth_headers,
    )
    assert second.status_code == 200
    assert second.json()["enrolled"] == 0
    assert second.json()["skipped_active"] == 1

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 1
    assert str(journeys[0].recipient_id) == rid


async def test_backfill_respects_cooldown(client, auth_headers):
    wid = await _create_and_activate_workflow(client, auth_headers, reentry_cooldown="P30D")
    rid = await _ingest_with_profile(client, auth_headers, email_domain="tx.cadencetests.io")

    first = await client.post(
        f"/api/workflows/{wid}/backfill",
        json={"segment": {"email_domain": "tx.cadencetests.io"}, "limit": 100},
        headers=auth_headers,
    )
    assert first.status_code == 200
    assert first.json()["enrolled"] == 1

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 1
    await _mark_terminal(
        journeys[0].id,
        status="completed",
        completed_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    second = await client.post(
        f"/api/workflows/{wid}/backfill",
        json={"segment": {"email_domain": "tx.cadencetests.io"}, "limit": 100},
        headers=auth_headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["enrolled"] == 0
    assert second.json()["skipped_cooldown"] == 1


async def test_backfill_returns_400_when_workflow_has_no_entry_node(client, auth_headers):
    bad_definition = {
        "version": 1,
        "name": "test-wf-no-entry",
        "vertical": "auto",
        "entry": "entry",
        "nodes": {
            "entry": {"type": "entry", "next": "exit_done"},
            "exit_done": {"type": "exit", "reason": "completed"},
        },
    }
    create = await client.post(
        "/api/workflows",
        json=_workflow_body(definition=bad_definition),
        headers=auth_headers,
    )
    wid = create.json()["id"]
    await client.post(f"/api/workflows/{wid}/activate", headers=auth_headers)

    async with async_session_factory() as s:
        await s.execute(
            text("UPDATE workflows SET definition = definition - 'entry' WHERE id = :id"),
            {"id": uuid.UUID(wid)},
        )
        await s.commit()

    resp = await client.post(
        f"/api/workflows/{wid}/backfill",
        json={"segment": {}, "limit": 10},
        headers=auth_headers,
    )
    assert resp.status_code == 400


async def test_backfill_invalid_segment_returns_400(client, auth_headers):
    wid = await _create_and_activate_workflow(client, auth_headers)
    resp = await client.post(
        f"/api/workflows/{wid}/backfill",
        json={"segment": {"all": "not-a-list"}, "limit": 10},
        headers=auth_headers,
    )
    assert resp.status_code == 400


async def test_backfill_requires_auth(client):
    resp = await client.post(
        f"/api/workflows/{uuid.uuid4()}/backfill",
        json={"segment": {}, "limit": 10},
    )
    assert resp.status_code == 401
