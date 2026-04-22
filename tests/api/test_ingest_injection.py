"""Tests for journey injection on recipient ingestion."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.core.db import async_session_factory
from app.models.journey import Journey
from app.models.workflow import Workflow


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


def _valid_definition() -> dict:
    return {
        "version": 1,
        "name": "test-wf",
        "vertical": "auto",
        "entry": "entry",
        "nodes": {
            "entry": {"type": "entry", "next": "exit_done"},
            "exit_done": {"type": "exit", "reason": "completed"},
        },
    }


def _workflow_body(entry_segment=None, **overrides) -> dict:
    body = {
        "name": f"test-wf-{uuid.uuid4()}",
        "tenant": "otto",
        "vertical": "auto",
        "definition": _valid_definition(),
        "entry_segment": entry_segment,
    }
    body.update(overrides)
    return body


async def _journeys_for_workflow(wid):
    async with async_session_factory() as s:
        return (
            (
                await s.execute(
                    select(Journey).where(Journey.workflow_id == uuid.UUID(wid))
                )
            )
            .scalars()
            .all()
        )


async def _activate(client, auth_headers, wid: str):
    resp = await client.post(f"/api/workflows/{wid}/activate", headers=auth_headers)
    assert resp.status_code == 200, resp.text


async def test_ingest_injects_journey(client, auth_headers):
    create = await client.post(
        "/api/workflows",
        json=_workflow_body(entry_segment={"vertical": "auto"}),
        headers=auth_headers,
    )
    assert create.status_code == 201
    wid = create.json()["id"]
    await _activate(client, auth_headers, wid)

    resp = await client.post(
        "/api/ingest/recipients",
        json=_recipient_payload(_email()),
        headers=auth_headers,
    )
    assert resp.status_code == 201

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 1
    assert journeys[0].status == "active"
    assert journeys[0].current_node_id == "entry"


async def test_ingest_skips_non_matching_segment(client, auth_headers):
    create = await client.post(
        "/api/workflows",
        json=_workflow_body(entry_segment={"vertical": "home"}),
        headers=auth_headers,
    )
    assert create.status_code == 201
    wid = create.json()["id"]
    await _activate(client, auth_headers, wid)

    resp = await client.post(
        "/api/ingest/recipients",
        json=_recipient_payload(_email(), vertical="auto"),
        headers=auth_headers,
    )
    assert resp.status_code == 201

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 0


async def test_ingest_skips_inactive_workflow(client, auth_headers):
    create = await client.post(
        "/api/workflows",
        json=_workflow_body(entry_segment={"vertical": "auto"}),
        headers=auth_headers,
    )
    assert create.status_code == 201
    wid = create.json()["id"]
    # Do NOT activate — remains draft.

    resp = await client.post(
        "/api/ingest/recipients",
        json=_recipient_payload(_email()),
        headers=auth_headers,
    )
    assert resp.status_code == 201

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 0


async def test_reingest_is_idempotent(client, auth_headers):
    create = await client.post(
        "/api/workflows",
        json=_workflow_body(entry_segment={"vertical": "auto"}),
        headers=auth_headers,
    )
    assert create.status_code == 201
    wid = create.json()["id"]
    await _activate(client, auth_headers, wid)

    email = _email()
    first = await client.post(
        "/api/ingest/recipients",
        json=_recipient_payload(email),
        headers=auth_headers,
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/ingest/recipients",
        json=_recipient_payload(email),
        headers=auth_headers,
    )
    assert second.status_code == 200

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 1
