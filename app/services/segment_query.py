"""SQL compiler for the segment DSL targeting contact_profile (SPEC-segmentation.md §7).

Placement rationale: CLAUDE.md forbids SQLAlchemy imports inside ``app/domain/``
(domain logic must stay pure/DB-free for unit testing). This compiler needs to
produce parameterized SQL, so it can't live there. A dedicated ``app/services/``
module (sibling to ``domain``, ``adapters``, ``api``) is the natural shared home.

``compile_segment`` takes a segment dict (raw v1/v2/v3 — it normalizes internally
via ``app.domain.segment.normalize``) and returns a ``(where_sql, params)`` tuple
suitable for use with ``sqlalchemy.text()``.  Tenant scoping is intentionally NOT
included — callers add ``cp.tenant = :tenant`` themselves.
"""
from __future__ import annotations

import re
import structlog
from datetime import datetime, timezone
from typing import Any

from app.core.config import EngagementConfig
from app.domain.isp import MAILBOX_PROVIDER_CODES as _ISP_MAILBOX_PROVIDER_CODES
from app.domain.isp import REPUTATION_GROUP_CODES as _ISP_REPUTATION_GROUP_CODES
from app.domain.segment import _parse_day_duration, normalize

__all__ = ["compile_segment"]

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Smallint code mappings
# ---------------------------------------------------------------------------

# Engagement tier codes (matches TIER_* constants in app/domain/engagement_v2.py).
ENGAGEMENT_TIER_CODES: dict[str, int] = {
    "active": 1,
    "engaged": 2,
    "passive": 3,
    "dormant": 4,
    "cold": 5,
    "new": 6,
    "unknown": 7,
}

# Provider codes are owned by app/domain/isp.py, which mirrors the
# domain_provider seed in seg1_domain_provider. Importing rather than
# re-declaring keeps the compiler and the ingest path from drifting.

# "outlook" is kept as a YAML-facing alias for microsoft; older segment files
# use it. There is no "unknown" provider — unclassified domains are "other" (0).
MAILBOX_PROVIDER_CODES: dict[str, int] = {
    **_ISP_MAILBOX_PROVIDER_CODES,
    "outlook": _ISP_MAILBOX_PROVIDER_CODES["microsoft"],
}

REPUTATION_GROUP_CODES: dict[str, int] = {
    **_ISP_REPUTATION_GROUP_CODES,
    "outlook": _ISP_REPUTATION_GROUP_CODES["microsoft"],
}

# Fields that carry smallint codes and their code lookup table.
_SMALLINT_FIELDS: dict[str, dict[str, int]] = {
    "mailbox_provider": MAILBOX_PROVIDER_CODES,
    "reputation_group": REPUTATION_GROUP_CODES,
    "engagement_tier": ENGAGEMENT_TIER_CODES,
}

# All contact_profile columns exposed to the segment DSL.
_CP_FIELDS: frozenset[str] = frozenset(
    {
        "email_domain",
        "mailbox_provider",
        "reputation_group",
        "engagement_tier",
        "country",
        "tz",
        "first_send_at",
        "last_send_at",
        "last_open_at",
        "last_click_at",
        "last_convert_at",
        "sends_30d",
        "opens_30d",
        "clicks_30d",
        "sends_90d",
        "opens_90d",
        "clicks_90d",
        "converts_90d",
        "bounces_90d",
        "complaints_90d",
        "sends_lifetime",
        "opens_lifetime",
        "clicks_lifetime",
        "engagement_score",
        "sendable",
    }
)


# ---------------------------------------------------------------------------
# Param bag
# ---------------------------------------------------------------------------


class _ParamBag:
    """Hands out unique parameter names and accumulates bind values."""

    def __init__(self) -> None:
        self._counter: int = 0
        self.params: dict[str, Any] = {}

    def add(self, value: Any) -> str:
        name = f"p{self._counter}"
        self._counter += 1
        self.params[name] = value
        return name


# ---------------------------------------------------------------------------
# Field → column resolution
# ---------------------------------------------------------------------------


_ATTR_PREFIX = "attributes."
_ATTR_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")

# Operators that compare numerically and therefore need a cast off jsonb text.
_NUMERIC_OPS = frozenset({"gt", "gte", "lt", "lte", "between"})


def _col(field: str) -> str:
    """Return the qualified ``cp.<column>`` expression for a DSL field name."""
    if field not in _CP_FIELDS:
        raise ValueError(f"unsupported segment field: {field!r}")
    return f"cp.{field}"


def _attr_subquery(key: str, bag: "_ParamBag", *, as_json: bool) -> str:
    """Correlated scalar subquery for a recipients.attributes key.

    Returns the raw jsonb value when ``as_json`` (needed by jsonb_exists and
    NULL checks), otherwise the text projection.  The key is bound as a
    parameter, never interpolated.
    """
    if not _ATTR_KEY_RE.match(key):
        raise ValueError(f"invalid attributes key: {key!r}")
    p = bag.add(key)
    op = "->" if as_json else "->>"
    return (
        f"(SELECT r.attributes {op} :{p} FROM recipients r"
        f" WHERE r.id = cp.contact_id AND r.tenant = cp.tenant)"
    )


def _coerce_value(field: str, value: Any) -> Any:
    """Translate string values to smallint codes for coded fields."""
    if field.startswith(_ATTR_PREFIX):
        return value
    codes = _SMALLINT_FIELDS.get(field)
    if codes is None:
        return value
    if isinstance(value, list):
        return [codes[v] if isinstance(v, str) else v for v in value]
    if isinstance(value, str):
        return codes[value]
    return value


# ---------------------------------------------------------------------------
# Clause compiler
# ---------------------------------------------------------------------------


def _compile_clause(node: dict, bag: _ParamBag, now: datetime) -> str:
    """Recursively compile a normalized clause node to a SQL fragment."""
    if "all" in node:
        parts = [_compile_clause(c, bag, now) for c in node["all"]]
        if not parts:
            return "true"
        if len(parts) == 1:
            return parts[0]
        return "(" + " AND ".join(parts) + ")"

    if "any" in node:
        parts = [_compile_clause(c, bag, now) for c in node["any"]]
        if not parts:
            return "false"
        if len(parts) == 1:
            return parts[0]
        return "(" + " OR ".join(parts) + ")"

    if "none" in node:
        parts = [_compile_clause(c, bag, now) for c in node["none"]]
        if not parts:
            return "true"
        inner = " OR ".join(parts) if len(parts) == 1 else "(" + " OR ".join(parts) + ")"
        return f"NOT ({inner})"

    # Field clause.
    field: str = node["field"]
    is_attr = field.startswith(_ATTR_PREFIX)
    if is_attr:
        attr_key = field[len(_ATTR_PREFIX):]
        # Validate eagerly so a bad key fails even for operators that build
        # their own expression below.
        if not _ATTR_KEY_RE.match(attr_key):
            raise ValueError(f"invalid attributes key: {attr_key!r}")
        op_name = next((k for k in node if k != "field"), "")
        if op_name in _NUMERIC_OPS:
            col = f"({_attr_subquery(attr_key, bag, as_json=False)})::numeric"
        elif op_name in ("is_null", "is_not_null", "contains"):
            col = _attr_subquery(attr_key, bag, as_json=True)
        else:
            col = _attr_subquery(attr_key, bag, as_json=False)
    else:
        col = _col(field)

    # --- Equality / set operators ---
    if "eq" in node:
        val = _coerce_value(field, node["eq"])
        p = bag.add(val)
        return f"{col} = :{p}"

    if "ne" in node:
        val = _coerce_value(field, node["ne"])
        p = bag.add(val)
        return f"{col} != :{p}"

    if "in" in node:
        val = _coerce_value(field, node["in"])
        p = bag.add(val)
        return f"{col} = ANY(:{p})"

    if "not_in" in node:
        val = _coerce_value(field, node["not_in"])
        p = bag.add(val)
        return f"{col} != ALL(:{p})"

    if "contains" in node:
        p = bag.add(node["contains"])
        # Function form, not the `?` operator: `?` collides with the driver's
        # placeholder syntax.
        return f"jsonb_exists({col}, :{p})"

    # --- Null-check operators ---
    if "is_null" in node:
        return f"{col} IS NULL"

    if "is_not_null" in node:
        return f"{col} IS NOT NULL"

    # --- Numeric / datetime comparison operators ---
    if "gt" in node:
        val = _coerce_value(field, node["gt"])
        p = bag.add(val)
        return f"{col} > :{p}"

    if "gte" in node:
        val = _coerce_value(field, node["gte"])
        p = bag.add(val)
        return f"{col} >= :{p}"

    if "lt" in node:
        val = _coerce_value(field, node["lt"])
        p = bag.add(val)
        return f"{col} < :{p}"

    if "lte" in node:
        val = _coerce_value(field, node["lte"])
        p = bag.add(val)
        return f"{col} <= :{p}"

    # --- Range operator ---
    if "between" in node:
        lo, hi = node["between"]
        lo_val = _coerce_value(field, lo)
        hi_val = _coerce_value(field, hi)
        p_lo = bag.add(lo_val)
        p_hi = bag.add(hi_val)
        return f"{col} BETWEEN :{p_lo} AND :{p_hi}"

    # --- Timestamp window operators ---
    if "within" in node:
        delta = _parse_day_duration(node["within"])
        cutoff = now - delta
        p = bag.add(cutoff)
        return f"{col} >= :{p}"

    if "not_within" in node:
        delta = _parse_day_duration(node["not_within"])
        cutoff = now - delta
        p = bag.add(cutoff)
        return f"({col} IS NULL OR {col} < :{p})"

    # --- Legacy before/after (ISO-8601 duration from now) ---
    if "before" in node:
        delta = _parse_day_duration(node["before"])
        cutoff = now - delta
        p = bag.add(cutoff)
        return f"{col} < :{p}"

    if "after" in node:
        delta = _parse_day_duration(node["after"])
        cutoff = now - delta
        p = bag.add(cutoff)
        return f"{col} > :{p}"

    raise ValueError(
        f"segment clause for field {field!r} has no recognized operator in node {node!r}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_segment(
    predicate_json: dict | None,
    config: EngagementConfig,
    *,
    now: datetime | None = None,
    acknowledges_cold: bool = False,
    include_unsendable: bool = False,
) -> tuple[str, dict]:
    """Compile a segment DSL dict into a parameterized SQL WHERE clause.

    Args:
        predicate_json: A raw v1-flat, v2, or v3 segment dict (or ``None``,
            matching everything). Normalized internally via
            ``app.domain.segment.normalize``.
        config: The loaded ``EngagementConfig`` (used for cold-exclusion guard).
        now: Reference time for relative-duration clauses; defaults to the
            current UTC time. Exposed for deterministic testing.
        acknowledges_cold: When ``True`` and ``config.cold_exclusion.enabled``
            is ``True``, the cold-tier exclusion guard is omitted.  Default
            ``False`` means the guard is always appended.
        include_unsendable: When ``True``, the ``cp.sendable = true`` guard is
            omitted so the caller can target unsendable contacts explicitly.

    Returns:
        A ``(where_sql, params)`` tuple.  ``where_sql`` is a parameterized SQL
        fragment (using ``:param_name`` style) suitable for
        ``WHERE {where_sql}``.  ``params`` is a dict of bind values for use
        with ``sqlalchemy.text()``.  Tenant scoping is NOT included — callers
        must add ``cp.tenant = :tenant`` themselves.

    Raises:
        ValueError: If ``predicate_json`` is structurally invalid (propagated
            from ``normalize()``) or references an unsupported field.
    """
    resolved_now = now if now is not None else datetime.now(timezone.utc)
    normalized = normalize(predicate_json)

    bag = _ParamBag()
    predicate_sql = _compile_clause(normalized, bag, resolved_now)

    guards: list[str] = []

    if not include_unsendable:
        guards.append("cp.sendable = true")

    if not acknowledges_cold and config.cold_exclusion.enabled:
        cold_code = ENGAGEMENT_TIER_CODES["cold"]
        cold_p = bag.add(cold_code)
        guards.append(f"cp.engagement_tier != :{cold_p}")

    all_parts: list[str] = []
    if predicate_sql != "true":
        all_parts.append(predicate_sql)
    all_parts.extend(guards)

    if not all_parts:
        where_sql = "true"
    elif len(all_parts) == 1:
        where_sql = all_parts[0]
    else:
        where_sql = " AND ".join(all_parts)

    log.debug(
        "segment_query.effective_predicate",
        where_sql=where_sql,
        acknowledges_cold=acknowledges_cold,
    )

    return where_sql, bag.params
