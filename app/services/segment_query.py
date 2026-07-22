"""SQL compiler for the segment DSL (SPEC_ADDENDUM_002.md §3.1).

Placement rationale: CLAUDE.md forbids SQLAlchemy imports inside
``app/domain/`` (domain logic must stay pure/DB-free for unit testing). This
compiler needs SQLAlchemy, so it can't live there. It also isn't a natural
fit inside ``app/api/segments.py`` as a "shared helper" — both
``app/api/workflows.py`` (backfill) and ``app/api/campaigns.py``
(segment-based send) need it too, and having them import from a sibling
``app/api`` module would create api-module-to-api-module coupling. A
dedicated ``app/services/`` module (sibling to ``domain``, ``adapters``,
``api``) is the natural shared home: it can depend on both the domain DSL
normalizer and SQLAlchemy, and every API module that needs segment-to-SQL
compilation imports it the same way.

``compile_segment`` takes a segment dict (raw v1/v2 — it normalizes
internally via ``app.domain.segment.normalize`` so callers don't have to)
and returns a SQLAlchemy boolean expression suitable for
``select(Recipient).where(compile_segment(...))``. Tenant scoping is
intentionally NOT included in the returned expression — see the module-level
note below — callers add ``Recipient.tenant == tenant`` themselves so the
compiled expression stays a pure function of the segment alone and every
call site is unambiguous about the tenant filter being present.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import ColumnElement, and_, not_, or_, select
from sqlalchemy.sql.selectable import Exists

from app.domain.duration import parse_iso8601_duration
from app.domain.segment import normalize
from app.models.event import Event
from app.models.recipient import Recipient

__all__ = ["compile_segment"]

_ENGAGEMENT_EVENT_TYPES = ("open", "click")


def compile_segment(
    segment: dict | None,
    *,
    now: datetime | None = None,
) -> ColumnElement[bool]:
    """Compile a segment DSL dict into a SQLAlchemy boolean expression.

    Args:
        segment: A raw v1-flat or v2 segment dict (or ``None``, matching
            everything). Normalized internally via
            ``app.domain.segment.normalize``.
        now: Reference time for relative-duration (``before``/``after``,
            ``no_engagement_since``) clauses; defaults to the current UTC
            time. Exposed for deterministic testing.

    Returns:
        A SQLAlchemy boolean expression against ``Recipient`` columns,
        suitable for ``select(Recipient).where(compile_segment(segment))``.
        Does NOT include tenant scoping — callers must add
        ``Recipient.tenant == tenant`` themselves.

    Raises:
        ValueError: If ``segment`` is structurally invalid (propagated from
            ``normalize()``).
    """
    resolved_now = now if now is not None else datetime.now(timezone.utc)
    normalized = normalize(segment)
    return _compile_node(normalized, resolved_now)


def _compile_node(node: dict, now: datetime) -> ColumnElement[bool]:
    if "all" in node:
        clauses = [_compile_node(c, now) for c in node["all"]]
        return and_(*clauses) if clauses else True
    if "any" in node:
        clauses = [_compile_node(c, now) for c in node["any"]]
        return or_(*clauses) if clauses else False
    if node.get("kind") == "no_engagement_since":
        return _compile_no_engagement_since(node, now)
    return _compile_field_clause(node, now)


def _compile_no_engagement_since(node: dict, now: datetime) -> Exists:
    window = parse_iso8601_duration(node["window"])
    since = now - window
    engagement_exists = (
        select(Event.id)
        .where(Event.recipient_id == Recipient.id)
        .where(Event.event_type.in_(_ENGAGEMENT_EVENT_TYPES))
        .where(Event.occurred_at >= since)
        .exists()
    )
    return not_(engagement_exists)


def _column_for_field(field: str) -> ColumnElement[Any]:
    if field == "vertical":
        return Recipient.vertical
    if field == "engagement_tier":
        return Recipient.engagement_tier
    if field == "last_seen_at":
        return Recipient.last_seen_at
    if field.startswith("attributes."):
        attr_key = field.split(".", 1)[1]
        return Recipient.attributes[attr_key].astext
    raise ValueError(f"unsupported segment field: {field!r}")


def _compile_field_clause(node: dict, now: datetime) -> ColumnElement[bool]:
    field = node["field"]
    column = _column_for_field(field)

    if "eq" in node:
        return column == node["eq"]
    if "in" in node:
        return column.in_(node["in"])
    if "not_in" in node:
        return column.not_in(node["not_in"])
    if "before" in node:
        window = parse_iso8601_duration(node["before"])
        return column < (now - window)
    if "after" in node:
        window = parse_iso8601_duration(node["after"])
        return column > (now - window)

    raise ValueError(f"segment clause for field {field!r} has no recognized operator")
