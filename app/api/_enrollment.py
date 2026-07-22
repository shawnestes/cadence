"""Shared per-recipient workflow enrollment logic.

Extracted from ``app/api/ingest.py`` so the same enroll-one-recipient
decision (re-entry cooldown check, active-journey uniqueness guard,
entry-node resolution) can be reused by both the ingest fan-out loop and the
``POST /api/workflows/{id}/backfill`` endpoint (``app/api/workflows.py``)
without duplicating it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import structlog
from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.duration import InvalidDurationError, parse_iso8601_duration
from app.models.base import uuid7
from app.models.journey import Journey
from app.models.workflow import Workflow

log = structlog.get_logger(__name__)

# Statuses that represent an in-flight journey. Must match the partial
# unique index on journeys (uq_journeys_active_workflow_recipient).
_ACTIVE_STATUSES = ("active", "scheduled", "waiting")


class EnrollmentOutcome(str, Enum):
    """Result of attempting to enroll one recipient into one workflow."""

    ENROLLED = "enrolled"
    SKIPPED_ACTIVE = "skipped_active"
    SKIPPED_COOLDOWN = "skipped_cooldown"
    SKIPPED_NO_ENTRY_NODE = "skipped_no_entry_node"


@dataclass(frozen=True)
class EnrollmentResult:
    """Outcome of a single ``enroll_recipient`` call."""

    outcome: EnrollmentOutcome
    journey_id: uuid.UUID | None = None


class _ReentryDecision(str, Enum):
    ALLOWED = "allowed"
    BLOCKED_ACTIVE = "blocked_active"
    BLOCKED_COOLDOWN = "blocked_cooldown"


async def _reentry_decision(
    db: AsyncSession, wf: Workflow, recipient_id: object, now: datetime
) -> _ReentryDecision:
    """Decide whether ``recipient_id`` may (re-)enter workflow ``wf``.

    Looks at the most recent journey for this (workflow, recipient) pair:

    - No prior journey at all -> allowed (first-time entry).
    - Prior journey is still in-flight (active/scheduled/waiting) -> not
      allowed; the caller's ON CONFLICT DO NOTHING guard also covers this
      for concurrent inserts, this is just an early skip.
    - Prior journey is terminal (completed/exited/errored) -> governed by
      ``wf.reentry_cooldown`` (SPEC §6.5): NULL means never re-enter;
      otherwise re-entry is allowed once the cooldown has elapsed since
      the terminal journey's completion timestamp.
    """
    latest = (
        await db.execute(
            select(Journey)
            .where(Journey.workflow_id == wf.id)
            .where(Journey.recipient_id == recipient_id)
            .order_by(desc(Journey.entered_at))
            .limit(1)
        )
    ).scalar_one_or_none()

    if latest is None:
        return _ReentryDecision.ALLOWED
    if latest.status in _ACTIVE_STATUSES:
        return _ReentryDecision.BLOCKED_ACTIVE

    # Terminal journey — apply the cooldown policy.
    if wf.reentry_cooldown is None:
        return _ReentryDecision.BLOCKED_COOLDOWN

    try:
        cooldown = parse_iso8601_duration(wf.reentry_cooldown)
    except InvalidDurationError as exc:
        # Should not happen — the schema validates on write — but don't
        # let a bad stored value silently permit unbounded re-entry.
        log.warning(
            "enrollment.invalid_reentry_cooldown",
            workflow_id=str(wf.id),
            reentry_cooldown=wf.reentry_cooldown,
            error=str(exc),
        )
        return _ReentryDecision.BLOCKED_COOLDOWN

    terminal_at = latest.completed_at or latest.entered_at
    if (now - terminal_at) >= cooldown:
        return _ReentryDecision.ALLOWED
    return _ReentryDecision.BLOCKED_COOLDOWN


async def enroll_recipient(
    db: AsyncSession,
    wf: Workflow,
    recipient_id: uuid.UUID,
    tenant: str,
    now: datetime,
) -> EnrollmentResult:
    """Attempt to enroll ``recipient_id`` into workflow ``wf`` as a journey.

    Applies the entry-node resolution, re-entry cooldown, and active-journey
    uniqueness checks shared by the ingest auto-enroll path and workflow
    backfill. Does not check suppression or segment matching — callers are
    responsible for those, since they differ between ingest (segment-gated,
    all active workflows) and backfill (single fixed workflow, operator-
    supplied segment, explicit suppression check).

    Commits the journey insert (or the no-op) on success; on failure, the
    caller's surrounding transaction is left for the caller to manage — this
    function itself only issues a single INSERT ... ON CONFLICT DO NOTHING
    and commits it.
    """
    definition = wf.definition or {}
    entry_node_id = definition.get("entry")
    if not entry_node_id:
        return EnrollmentResult(outcome=EnrollmentOutcome.SKIPPED_NO_ENTRY_NODE)

    decision = await _reentry_decision(db, wf, recipient_id, now)
    if decision == _ReentryDecision.BLOCKED_ACTIVE:
        return EnrollmentResult(outcome=EnrollmentOutcome.SKIPPED_ACTIVE)
    if decision == _ReentryDecision.BLOCKED_COOLDOWN:
        return EnrollmentResult(outcome=EnrollmentOutcome.SKIPPED_COOLDOWN)

    journey_id = uuid7()
    # Use Core insert with on_conflict_do_nothing to avoid the duplicate-key
    # IntegrityError / async unwind / attribute refresh dance that violates
    # the greenlet boundary. The partial unique index on
    # (workflow_id, recipient_id) only enforces on non-terminal statuses;
    # _reentry_allowed above applies the cooldown policy (SPEC §6.5). This
    # guard remains as a concurrency backstop against a race between the
    # check and the insert.
    jstmt = (
        pg_insert(Journey)
        .values(
            id=journey_id,
            tenant=tenant,
            workflow_id=wf.id,
            workflow_version=wf.version,
            recipient_id=recipient_id,
            current_node_id=entry_node_id,
            next_action_at=now,
            status="active",
            context={},
            entered_at=now,
        )
        .on_conflict_do_nothing()
        .returning(Journey.id)
    )
    result = await db.execute(jstmt)
    inserted = result.scalar_one_or_none()
    await db.commit()

    if inserted is None:
        # Conflict — another in-flight journey already exists for this pair.
        return EnrollmentResult(outcome=EnrollmentOutcome.SKIPPED_ACTIVE)

    return EnrollmentResult(outcome=EnrollmentOutcome.ENROLLED, journey_id=inserted)
