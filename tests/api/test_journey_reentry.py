"""Re-entry after journey completion (SPEC §6.5).

Re-entry policy is governed by `workflows.reentry_cooldown` (an ISO-8601
duration, or NULL). Per spec, the default (NULL) means a recipient never
re-enters a workflow once their journey has reached a terminal status
(completed/exited/errored). A configured cooldown (e.g. "P30D") allows
re-entry once that much time has elapsed since the prior journey's
completion; "PT0S" allows immediate re-entry.

Re-entry is enforced in the ingest path (app/api/ingest.py::_reentry_allowed),
so these tests drive it through POST /api/ingest/recipients rather than
inserting Journey rows directly — that exercises the real decision logic,
not just the DB constraint.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.core.db import async_session_factory
from app.models.journey import Journey
from app.models.recipient import Recipient
from app.models.workflow import Workflow
from app.models.workflow_version import WorkflowVersion


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
        "entry_segment": {"vertical": "auto"},
        "reentry_cooldown": reentry_cooldown,
    }
    body.update(overrides)
    return body


async def _activate(client, auth_headers, wid: str) -> None:
    resp = await client.post(f"/api/workflows/{wid}/activate", headers=auth_headers)
    assert resp.status_code == 200, resp.text


async def _journeys_for_workflow(wid: str) -> list[Journey]:
    async with async_session_factory() as s:
        return (
            (await s.execute(select(Journey).where(Journey.workflow_id == uuid.UUID(wid))))
            .scalars()
            .all()
        )


async def _mark_terminal(journey_id, *, status: str, completed_at: datetime) -> None:
    """Directly backdate a journey to a terminal status, bypassing the worker."""
    async with async_session_factory() as s:
        await s.execute(
            text(
                "UPDATE journeys SET status = :status, completed_at = :completed_at, "
                "next_action_at = NULL WHERE id = :id"
            ),
            {"status": status, "completed_at": completed_at, "id": journey_id},
        )
        await s.commit()


async def test_default_cooldown_never_reenters_after_completion(client, auth_headers):
    """NULL reentry_cooldown (the spec default) blocks re-entry forever."""
    create = await client.post(
        "/api/workflows", json=_workflow_body(reentry_cooldown=None), headers=auth_headers
    )
    assert create.status_code == 201, create.text
    assert create.json()["reentry_cooldown"] is None
    wid = create.json()["id"]
    await _activate(client, auth_headers, wid)

    email = _email()
    first = await client.post(
        "/api/ingest/recipients", json=_recipient_payload(email), headers=auth_headers
    )
    assert first.status_code == 201

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 1
    await _mark_terminal(
        journeys[0].id, status="completed", completed_at=datetime.now(timezone.utc)
    )

    second = await client.post(
        "/api/ingest/recipients", json=_recipient_payload(email), headers=auth_headers
    )
    assert second.status_code == 200

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 1
    assert journeys[0].status == "completed"


async def test_p30d_cooldown_blocks_within_window(client, auth_headers):
    create = await client.post(
        "/api/workflows", json=_workflow_body(reentry_cooldown="P30D"), headers=auth_headers
    )
    assert create.status_code == 201, create.text
    wid = create.json()["id"]
    await _activate(client, auth_headers, wid)

    email = _email()
    first = await client.post(
        "/api/ingest/recipients", json=_recipient_payload(email), headers=auth_headers
    )
    assert first.status_code == 201
    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 1

    # Completed 1 day ago — well within the 30-day cooldown window.
    await _mark_terminal(
        journeys[0].id,
        status="completed",
        completed_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    second = await client.post(
        "/api/ingest/recipients", json=_recipient_payload(email), headers=auth_headers
    )
    assert second.status_code == 200

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 1, "should not re-enter inside the cooldown window"


async def test_p30d_cooldown_allows_reentry_after_window(client, auth_headers):
    create = await client.post(
        "/api/workflows", json=_workflow_body(reentry_cooldown="P30D"), headers=auth_headers
    )
    assert create.status_code == 201, create.text
    wid = create.json()["id"]
    await _activate(client, auth_headers, wid)

    email = _email()
    first = await client.post(
        "/api/ingest/recipients", json=_recipient_payload(email), headers=auth_headers
    )
    assert first.status_code == 201
    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 1

    # Completed 31 days ago — past the 30-day cooldown window.
    await _mark_terminal(
        journeys[0].id,
        status="completed",
        completed_at=datetime.now(timezone.utc) - timedelta(days=31),
    )

    second = await client.post(
        "/api/ingest/recipients", json=_recipient_payload(email), headers=auth_headers
    )
    assert second.status_code == 200

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 2, "should re-enter once the cooldown window has elapsed"
    statuses = sorted(j.status for j in journeys)
    assert statuses == ["active", "completed"]


async def test_pt0s_cooldown_allows_immediate_reentry(client, auth_headers):
    create = await client.post(
        "/api/workflows", json=_workflow_body(reentry_cooldown="PT0S"), headers=auth_headers
    )
    assert create.status_code == 201, create.text
    wid = create.json()["id"]
    await _activate(client, auth_headers, wid)

    email = _email()
    first = await client.post(
        "/api/ingest/recipients", json=_recipient_payload(email), headers=auth_headers
    )
    assert first.status_code == 201
    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 1

    await _mark_terminal(
        journeys[0].id, status="completed", completed_at=datetime.now(timezone.utc)
    )

    second = await client.post(
        "/api/ingest/recipients", json=_recipient_payload(email), headers=auth_headers
    )
    assert second.status_code == 200

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 2, "PT0S should allow immediate re-entry"


async def test_concurrent_active_journey_still_blocked(client, auth_headers, db_session):
    """A second active journey for the same (workflow, recipient) is never
    allowed, cooldown notwithstanding — enforced by the partial unique index.
    """
    wf = Workflow(
        tenant="otto",
        name=f"test-reentry-{uuid.uuid4()}",
        vertical="auto",
        status="active",
        definition=_definition(),
        entry_segment=None,
        version=1,
        reentry_cooldown="PT0S",
    )
    db_session.add(wf)
    await db_session.commit()
    await db_session.refresh(wf)

    now = datetime.now(timezone.utc)
    rcp = Recipient(
        tenant="otto",
        email=_email(),
        external_id="otto_test",
        vertical="auto",
        first_seen_at=now,
        last_seen_at=now,
        consent_source="otto_form",
        consent_at=now,
        attributes={},
    )
    db_session.add(rcp)
    await db_session.commit()
    await db_session.refresh(rcp)

    j1 = Journey(
        tenant="otto",
        workflow_id=wf.id,
        workflow_version=wf.version,
        recipient_id=rcp.id,
        current_node_id="entry",
        next_action_at=now,
        status="active",
        entered_at=now,
    )
    db_session.add(j1)
    await db_session.commit()

    j2 = Journey(
        tenant="otto",
        workflow_id=wf.id,
        workflow_version=wf.version,
        recipient_id=rcp.id,
        current_node_id="entry",
        next_action_at=now,
        status="active",
        entered_at=now,
    )
    db_session.add(j2)
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_workflow_version_pinning_across_edit(client, auth_headers):
    """Recipients pin to the workflow version in effect when they enter.

    A recipient who entered under v1 keeps resolving the v1 definition even
    after the workflow is edited to v2; a recipient enrolled after the edit
    resolves v2.
    """
    create = await client.post(
        "/api/workflows",
        json=_workflow_body(reentry_cooldown=None, definition=_definition("v1_reason")),
        headers=auth_headers,
    )
    assert create.status_code == 201, create.text
    wid = create.json()["id"]
    assert create.json()["version"] == 1
    await _activate(client, auth_headers, wid)

    email_a = _email()
    resp_a = await client.post(
        "/api/ingest/recipients", json=_recipient_payload(email_a), headers=auth_headers
    )
    assert resp_a.status_code == 201

    update = await client.patch(
        f"/api/workflows/{wid}",
        json={"definition": _definition("v2_reason")},
        headers=auth_headers,
    )
    assert update.status_code == 200, update.text
    assert update.json()["version"] == 2

    email_b = _email()
    resp_b = await client.post(
        "/api/ingest/recipients", json=_recipient_payload(email_b), headers=auth_headers
    )
    assert resp_b.status_code == 201

    journeys = await _journeys_for_workflow(wid)
    assert len(journeys) == 2
    journeys_by_version = {j.workflow_version: j for j in journeys}
    assert set(journeys_by_version) == {1, 2}

    async with async_session_factory() as s:
        versions = (
            (
                await s.execute(
                    select(WorkflowVersion)
                    .where(WorkflowVersion.workflow_id == uuid.UUID(wid))
                    .order_by(WorkflowVersion.version)
                )
            )
            .scalars()
            .all()
        )
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].definition["nodes"]["exit_done"]["reason"] == "v1_reason"
    assert versions[1].definition["nodes"]["exit_done"]["reason"] == "v2_reason"

    v1_snapshot = next(v for v in versions if v.version == journeys_by_version[1].workflow_version)
    v2_snapshot = next(v for v in versions if v.version == journeys_by_version[2].workflow_version)
    assert v1_snapshot.definition["nodes"]["exit_done"]["reason"] == "v1_reason"
    assert v2_snapshot.definition["nodes"]["exit_done"]["reason"] == "v2_reason"
