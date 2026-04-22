"""Tests for app.workers.workflow_tick.run_once."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.db import async_session_factory
from app.models.base import uuid7
from app.models.campaign import Campaign
from app.models.event import Event
from app.models.journey import Journey
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.template import Template
from app.models.workflow import Workflow
from app.workers import workflow_tick


async def _seed_recipient(db_session, status: str = "active") -> Recipient:
    now = datetime.now(timezone.utc)
    r = Recipient(
        tenant="otto",
        email=f"test-{uuid.uuid4()}@cadencetests.io",
        external_id="otto_test",
        vertical="auto",
        first_seen_at=now,
        last_seen_at=now,
        consent_source="otto_form",
        consent_at=now,
        attributes={"first_name": "Sarah"},
        status=status,
    )
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)
    return r


async def _seed_workflow(db_session, definition: dict, status: str = "active") -> Workflow:
    wf = Workflow(
        tenant="otto",
        name=f"test-wf-{uuid.uuid4()}",
        vertical="auto",
        status=status,
        definition=definition,
        entry_segment=None,
        version=1,
    )
    db_session.add(wf)
    await db_session.commit()
    await db_session.refresh(wf)
    return wf


async def _seed_journey(db_session, wf: Workflow, recipient: Recipient, node_id: str) -> Journey:
    now = datetime.now(timezone.utc)
    j = Journey(
        tenant="otto",
        workflow_id=wf.id,
        workflow_version=wf.version,
        recipient_id=recipient.id,
        current_node_id=node_id,
        next_action_at=now,
        status="active",
        entered_at=now,
    )
    db_session.add(j)
    await db_session.commit()
    await db_session.refresh(j)
    return j


async def _seed_campaign_and_template(db_session) -> tuple[Campaign, Template]:
    c = Campaign(tenant="otto", name=f"test-campaign-{uuid.uuid4()}", vertical="auto")
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    t = Template(
        tenant="otto",
        campaign_id=c.id,
        cohort_key=f"test-cohort-{uuid.uuid4()}",
        variant_label="A",
        subject="Hi",
        body_html="<p>Hi</p>",
        body_text="Hi",
        status="approved",
    )
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return c, t


async def _read_journey(jid):
    async with async_session_factory() as s:
        return (await s.execute(select(Journey).where(Journey.id == jid))).scalar_one_or_none()


async def _read_sends_for_journey(jid):
    async with async_session_factory() as s:
        return (
            (await s.execute(select(Send).where(Send.journey_id == jid))).scalars().all()
        )


def _linear_def(name: str = "test-linear") -> dict:
    return {
        "version": 1,
        "name": name,
        "vertical": "auto",
        "entry": "entry",
        "nodes": {
            "entry": {"type": "entry", "next": "exit_done"},
            "exit_done": {"type": "exit", "reason": "completed"},
        },
    }


async def test_entry_node_advances_immediately(db_session):
    r = await _seed_recipient(db_session)
    wf = await _seed_workflow(db_session, _linear_def())
    j = await _seed_journey(db_session, wf, r, "entry")

    await workflow_tick.run_once()
    # Entry advances to exit; exit completes on a second tick.
    await workflow_tick.run_once()

    fresh = await _read_journey(j.id)
    assert fresh.status == "completed"
    assert fresh.completed_at is not None


async def test_wait_node_sets_next_action_at(db_session):
    wf_def = {
        "version": 1,
        "name": "test-wait",
        "vertical": "auto",
        "entry": "entry",
        "nodes": {
            "entry": {"type": "entry", "next": "wait"},
            "wait": {"type": "wait", "duration": "P1D", "next": "exit_done"},
            "exit_done": {"type": "exit", "reason": "completed"},
        },
    }
    r = await _seed_recipient(db_session)
    wf = await _seed_workflow(db_session, wf_def)
    j = await _seed_journey(db_session, wf, r, "entry")

    # Tick: entry → wait, set next_action to wait. Then wait executes → next_action = now+1d.
    await workflow_tick.run_once()
    await workflow_tick.run_once()

    fresh = await _read_journey(j.id)
    # current node should now be exit_done (wait advanced its current_node_id to next).
    assert fresh.current_node_id == "exit_done"
    assert fresh.status == "active"
    assert fresh.next_action_at is not None
    delta = fresh.next_action_at - datetime.now(timezone.utc)
    # Approximately 1 day from now.
    assert timedelta(hours=23) < delta < timedelta(hours=25)


async def test_send_node_enqueues_send(db_session):
    r = await _seed_recipient(db_session)
    c, t = await _seed_campaign_and_template(db_session)
    wf_def = {
        "version": 1,
        "name": "test-send",
        "vertical": "auto",
        "entry": "entry",
        "nodes": {
            "entry": {"type": "entry", "next": "send"},
            "send": {"type": "send", "campaign_id": str(c.id), "next": "exit_done"},
            "exit_done": {"type": "exit", "reason": "completed"},
        },
    }
    wf = await _seed_workflow(db_session, wf_def)
    j = await _seed_journey(db_session, wf, r, "entry")

    # entry → send → exit takes multiple ticks.
    for _ in range(3):
        await workflow_tick.run_once()

    sends = await _read_sends_for_journey(j.id)
    assert len(sends) == 1
    assert sends[0].status == "queued"
    assert sends[0].campaign_id == c.id
    assert sends[0].recipient_id == r.id


async def test_exit_node_completes_journey(db_session):
    r = await _seed_recipient(db_session)
    wf = await _seed_workflow(db_session, _linear_def())
    j = await _seed_journey(db_session, wf, r, "exit_done")

    await workflow_tick.run_once()

    fresh = await _read_journey(j.id)
    assert fresh.status == "completed"
    assert fresh.completed_at is not None
    assert fresh.next_action_at is None


async def test_global_exit_suppressed_recipient(db_session):
    r = await _seed_recipient(db_session, status="suppressed")
    wf = await _seed_workflow(db_session, _linear_def())
    j = await _seed_journey(db_session, wf, r, "entry")

    await workflow_tick.run_once()

    fresh = await _read_journey(j.id)
    assert fresh.status == "exited"
    assert fresh.completed_at is not None


async def test_global_exit_paused_workflow(db_session):
    r = await _seed_recipient(db_session)
    wf = await _seed_workflow(db_session, _linear_def(), status="paused")
    j = await _seed_journey(db_session, wf, r, "entry")

    await workflow_tick.run_once()

    fresh = await _read_journey(j.id)
    assert fresh.status == "exited"


async def test_branch_event_in_window_true(db_session):
    wf_def = {
        "version": 1,
        "name": "test-branch",
        "vertical": "auto",
        "entry": "entry",
        "nodes": {
            "entry": {"type": "entry", "next": "br"},
            "br": {
                "type": "branch",
                "condition": {
                    "kind": "event_in_window",
                    "event_type": "open",
                    "since_node": "journey_start",
                    "window": "P1D",
                },
                "if_true": "exit_yes",
                "if_false": "exit_no",
            },
            "exit_yes": {"type": "exit", "reason": "engaged"},
            "exit_no": {"type": "exit", "reason": "no_engagement"},
        },
    }
    r = await _seed_recipient(db_session)
    wf = await _seed_workflow(db_session, wf_def)
    j = await _seed_journey(db_session, wf, r, "entry")

    now = datetime.now(timezone.utc)
    ev = Event(
        id=uuid7(),
        tenant="otto",
        recipient_id=r.id,
        event_type="open",
        occurred_at=now,
    )
    db_session.add(ev)
    await db_session.commit()

    for _ in range(5):
        await workflow_tick.run_once()
        fresh = await _read_journey(j.id)
        if fresh.status == "completed":
            break

    fresh = await _read_journey(j.id)
    assert fresh.status == "completed"
    assert fresh.current_node_id == "exit_yes"
