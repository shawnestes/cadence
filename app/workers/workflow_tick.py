"""Workflow tick worker: advances active journeys through their workflow graph.

Per SPEC §6.4 (tick loop), §6.6 (global exits, branch conditions, re-entry).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.esp.factory import get_esp_adapter
from app.core.db import async_session_factory
from app.domain.workflow_defs import (
    BranchNode,
    ClickUrlMatchCondition,
    CompositeCondition,
    EntryNode,
    EventInWindowCondition,
    ExitNode,
    SendNode,
    WaitNode,
    WorkflowDefinition,
    parse_iso_duration,
)
from app.models.event import Event
from app.models.journey import Journey
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.template import Template
from app.models.workflow import Workflow

log = structlog.get_logger(__name__)

_GLOBAL_EXIT_STATUSES = {"suppressed", "bounced_hard", "complained"}


async def run_once() -> dict[str, int]:
    processed = 0
    advanced = 0
    exited_global = 0
    errored = 0

    now = datetime.now(timezone.utc)

    async with async_session_factory() as session:
        stmt = (
            select(Journey)
            .where(Journey.status == "active")
            .where((Journey.next_action_at.is_(None)) | (Journey.next_action_at <= now))
            .order_by(Journey.next_action_at.asc().nulls_first())
            .limit(1000)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        journeys = list(result.scalars().all())

        for journey in journeys:
            processed += 1
            log_ctx = dict(
                journey_id=str(journey.id),
                recipient_id=str(journey.recipient_id),
                tenant=journey.tenant,
                workflow_id=str(journey.workflow_id),
                node_id=journey.current_node_id,
            )
            try:
                # Global exit checks.
                recipient = await session.get(Recipient, journey.recipient_id)
                if recipient is None or recipient.status in _GLOBAL_EXIT_STATUSES:
                    journey.status = "exited"
                    journey.completed_at = now
                    journey.next_action_at = None
                    exited_global += 1
                    await session.commit()
                    log.info("workflow_tick.exited_global", reason="recipient_status", **log_ctx)
                    continue

                workflow = await session.get(Workflow, journey.workflow_id)
                if workflow is None:
                    journey.status = "errored"
                    await session.commit()
                    errored += 1
                    log.warning("workflow_tick.workflow_missing", **log_ctx)
                    continue

                # TODO(M3.1+): snapshot workflow.definition by version; for now we
                # always use the current workflow definition even if
                # workflow.version != journey.workflow_version.
                if workflow.status in {"paused", "archived"}:
                    journey.status = "exited"
                    journey.completed_at = now
                    journey.next_action_at = None
                    exited_global += 1
                    await session.commit()
                    log.info(
                        "workflow_tick.exited_global",
                        reason=f"workflow_{workflow.status}",
                        **log_ctx,
                    )
                    continue

                definition = WorkflowDefinition.model_validate(workflow.definition)
                node = definition.nodes.get(journey.current_node_id)
                if node is None:
                    journey.status = "errored"
                    await session.commit()
                    errored += 1
                    log.warning("workflow_tick.node_missing", **log_ctx)
                    continue

                await _execute_node(node, journey, workflow, definition, session, now)
                advanced += 1
                await session.commit()
                log.info(
                    "workflow_tick.advanced",
                    node_type=node.type,
                    new_node_id=journey.current_node_id,
                    status=journey.status,
                    **{k: v for k, v in log_ctx.items() if k != "node_id"},
                )
            except Exception:  # noqa: BLE001
                await session.rollback()
                try:
                    fresh = await session.get(Journey, journey.id)
                    if fresh is not None:
                        fresh.status = "errored"
                        await session.commit()
                except Exception:  # noqa: BLE001
                    await session.rollback()
                errored += 1
                log.exception("workflow_tick.error", **log_ctx)

    return {
        "processed": processed,
        "advanced": advanced,
        "exited_global": exited_global,
        "errored": errored,
    }


async def _execute_node(
    node: Any,
    journey: Journey,
    workflow: Workflow,
    definition: WorkflowDefinition,
    session: AsyncSession,
    now: datetime,
) -> None:
    if isinstance(node, EntryNode):
        journey.current_node_id = node.next
        journey.next_action_at = now
        return

    if isinstance(node, SendNode):
        await _enqueue_send(node, journey, workflow, session, now)
        journey.current_node_id = node.next
        journey.next_action_at = now
        return

    if isinstance(node, WaitNode):
        duration = parse_iso_duration(node.duration)
        journey.current_node_id = node.next
        journey.next_action_at = now + duration
        return

    if isinstance(node, BranchNode):
        result = await _evaluate_condition(
            node.condition, journey, workflow, definition, session, now
        )
        journey.current_node_id = node.if_true if result else node.if_false
        journey.next_action_at = now
        return

    if isinstance(node, ExitNode):
        journey.status = "completed"
        journey.completed_at = now
        journey.next_action_at = None
        return

    raise ValueError(f"unknown node type: {type(node).__name__}")


async def _enqueue_send(
    node: SendNode,
    journey: Journey,
    workflow: Workflow,
    session: AsyncSession,
    now: datetime,
) -> None:
    tmpl_stmt = (
        select(Template)
        .where(Template.tenant == journey.tenant)
        .where(Template.campaign_id == node.campaign_id)
        .where(Template.status == "approved")
        .limit(1)
    )
    template = (await session.execute(tmpl_stmt)).scalar_one_or_none()
    if template is None:
        log.warning(
            "workflow_tick.no_approved_template",
            journey_id=str(journey.id),
            tenant=journey.tenant,
            campaign_id=node.campaign_id,
            node_id=journey.current_node_id,
        )
        return

    esp = get_esp_adapter()
    send = Send(
        tenant=journey.tenant,
        journey_id=journey.id,
        campaign_id=template.campaign_id,
        template_id=template.id,
        recipient_id=journey.recipient_id,
        cohort_key=template.cohort_key,
        variant_label=template.variant_label,
        esp=esp.name,
        rendered_subject=template.subject,
        status="queued",
        queued_at=now,
    )
    session.add(send)


async def _evaluate_condition(
    cond: Any,
    journey: Journey,
    workflow: Workflow,
    definition: WorkflowDefinition,
    session: AsyncSession,
    now: datetime,
) -> bool:
    if isinstance(cond, CompositeCondition):
        if cond.kind == "and":
            for sub in cond.operands:
                if not await _evaluate_condition(
                    sub, journey, workflow, definition, session, now
                ):
                    return False
            return True
        # or
        for sub in cond.operands:
            if await _evaluate_condition(
                sub, journey, workflow, definition, session, now
            ):
                return True
        return False

    window = parse_iso_duration(cond.window)
    window_start = now - window
    reference_time = await _reference_time(cond.since_node, journey, definition, session)
    effective_start = max(window_start, reference_time) if reference_time else window_start

    if isinstance(cond, EventInWindowCondition):
        if cond.event_type == "open":
            types = ["open"]
        elif cond.event_type == "click":
            types = ["click"]
        else:
            types = ["open", "click"]
        stmt = (
            select(Event.id)
            .where(Event.tenant == journey.tenant)
            .where(Event.recipient_id == journey.recipient_id)
            .where(Event.event_type.in_(types))
            .where(and_(Event.occurred_at >= effective_start, Event.occurred_at <= now))
            .limit(1)
        )
        row = (await session.execute(stmt)).first()
        return row is not None

    if isinstance(cond, ClickUrlMatchCondition):
        pattern = f"%{cond.url_contains}%"
        stmt = (
            select(Event.id)
            .where(Event.tenant == journey.tenant)
            .where(Event.recipient_id == journey.recipient_id)
            .where(Event.event_type == "click")
            .where(Event.url.ilike(pattern))
            .where(and_(Event.occurred_at >= effective_start, Event.occurred_at <= now))
            .limit(1)
        )
        row = (await session.execute(stmt)).first()
        return row is not None

    raise ValueError(f"unknown condition kind: {type(cond).__name__}")


async def _reference_time(
    since_node: str,
    journey: Journey,
    definition: WorkflowDefinition,
    session: AsyncSession,
) -> datetime:
    if since_node == "journey_start":
        return journey.entered_at

    if since_node == "any_send":
        stmt = (
            select(Send.sent_at)
            .where(Send.journey_id == journey.id)
            .where(Send.sent_at.is_not(None))
            .order_by(Send.sent_at.asc())
            .limit(1)
        )
        earliest = (await session.execute(stmt)).scalar_one_or_none()
        return earliest or journey.entered_at

    node = definition.nodes.get(since_node)
    if isinstance(node, SendNode):
        stmt = (
            select(Send.sent_at)
            .where(Send.journey_id == journey.id)
            .where(Send.campaign_id == node.campaign_id)
            .where(Send.sent_at.is_not(None))
            .order_by(Send.sent_at.desc())
            .limit(1)
        )
        latest = (await session.execute(stmt)).scalar_one_or_none()
        return latest or journey.entered_at

    return journey.entered_at
