"""Pure segment DSL: normalization and per-recipient evaluation.

v1 supported a tiny flat dict DSL (see below). v2 (SPEC_ADDENDUM_002.md §3.1)
adds explicit boolean nesting (``all``/``any``), comparison operators
(``eq``/``in``/``not_in``/``before``/``after``), and a
``no_engagement_since`` clause kind. v3 (SPEC-segmentation.md §7) adds the
``none`` combinator and a full operator set for contact_profile fields.

**Clause shapes accepted by normalize()/matches():**

*Old key-operator style* (v1/v2, still accepted for backward compat):
  ``{"field": "X", "eq": "Y"}``  — op name is the key, value is the dict value

*New field/op/value style* (v3):
  ``{"field": "X", "op": "within", "value": "30d"}``  — op is a string value

Both shapes are normalized to the old key-operator internal form so that
a single evaluation path handles both.  The new style must be used for
operators that only exist in v3 (``ne``, ``gt``, ``gte``, ``lt``, ``lte``,
``within``, ``not_within``, ``is_null``, ``is_not_null``, ``between``).

**Combinators:** ``all`` (AND), ``any`` (OR), ``none`` (NOT ANY). All nestable.

**Field whitelist for new v3 DSL fields** (contact_profile columns):
  ``email_domain``, ``mailbox_provider``, ``reputation_group``, ``country``,
  ``tz``, ``first_send_at``, ``last_send_at``, ``last_open_at``,
  ``last_click_at``, ``last_convert_at``, ``sends_30d``, ``opens_30d``,
  ``clicks_30d``, ``sends_90d``, ``opens_90d``, ``clicks_90d``,
  ``converts_90d``, ``bounces_90d``, ``complaints_90d``, ``sends_lifetime``,
  ``opens_lifetime``, ``clicks_lifetime``, ``engagement_score``,
  ``engagement_tier``, ``consent_state``, ``sendable``.

No FastAPI or SQLAlchemy imports — this module must stay pure per CLAUDE.md
so it is usable both for per-recipient evaluation and (via
``app/services/segment_query.py``) as the source of truth normalized by the
SQL compiler.
"""
from __future__ import annotations

import re
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any

from app.domain.duration import InvalidDurationError, parse_iso8601_duration

__all__ = ["normalize", "matches"]

# ---------------------------------------------------------------------------
# Operator sets
# ---------------------------------------------------------------------------

# Old key-operator style (v1/v2): the op name is a key in the clause dict.
_KEY_OPERATORS = ("eq", "in", "not_in", "before", "after")

# New v3 operators that only exist in the {field, op, value} style.
_V3_OPERATORS = (
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "within",
    "not_within",
    "is_null",
    "is_not_null",
    "between",
)

# All recognized operator names (used when "op" key is present).
_ALL_OPERATORS = set(_KEY_OPERATORS) | set(_V3_OPERATORS)

# Keys allowed in a key-operator-style clause.
_FIELD_CLAUSE_KEYS = {"field", "eq", "in", "not_in", "before", "after"}

# Operators that require no value (null-check operators).
_NO_VALUE_OPS = {"is_null", "is_not_null"}

# Pattern for simple day-duration strings like "30d", "90d".
_DAY_DURATION_RE = re.compile(r"^(\d+)d$", re.IGNORECASE)

# Contact-profile field whitelist for v3 clauses.
_V3_FIELDS = frozenset(
    {
        "email_domain",
        "mailbox_provider",
        "reputation_group",
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
        "engagement_tier",
        "consent_state",
        "sendable",
    }
)


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------


def _parse_day_duration(value: str) -> timedelta:
    """Parse a simple day-count duration string (``Nd``) into a timedelta.

    Falls back to ``parse_iso8601_duration`` when the input does not match
    the ``Nd`` shorthand so that ISO-8601 strings like ``P30D`` also work.

    Args:
        value: A string like ``"30d"`` or ``"P30D"``.

    Returns:
        The equivalent timedelta.

    Raises:
        ValueError: If the string matches neither pattern.
    """
    m = _DAY_DURATION_RE.match(value.strip())
    if m:
        return timedelta(days=int(m.group(1)))
    try:
        return parse_iso8601_duration(value)
    except InvalidDurationError as exc:
        raise ValueError(
            f"duration must be a day-count like '30d' or an ISO-8601 string, got {value!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Normalize helpers
# ---------------------------------------------------------------------------


def _normalize_v3_clause(clause: dict) -> dict:
    """Validate and normalize a v3 {field, op, value} clause.

    Converts to the internal key-operator representation so a single
    evaluation path handles both old and new styles:
      ``{"field": "last_open_at", "op": "within", "value": "30d"}``
      ->  ``{"field": "last_open_at", "within": "30d"}``
    """
    field = clause["field"]
    op: str = clause["op"]

    if op not in _ALL_OPERATORS:
        raise ValueError(
            f"unknown operator {op!r} in segment clause; "
            f"recognized operators: {sorted(_ALL_OPERATORS)}"
        )

    if op in _NO_VALUE_OPS:
        if "value" in clause:
            raise ValueError(
                f"operator {op!r} must not have a 'value' key, got {clause!r}"
            )
        extra = set(clause) - {"field", "op"}
        if extra:
            raise ValueError(
                f"unexpected keys in {op!r} clause: {sorted(extra)}"
            )
        return {"field": field, op: True}

    if "value" not in clause:
        raise ValueError(
            f"operator {op!r} requires a 'value' key in clause {clause!r}"
        )
    value = clause["value"]
    extra = set(clause) - {"field", "op", "value"}
    if extra:
        raise ValueError(f"unexpected keys in clause: {sorted(extra)}")

    # Operator-specific value validation.
    if op in ("in", "not_in"):
        if not isinstance(value, list):
            raise ValueError(f"operator {op!r} requires a list value, got {value!r}")
    elif op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError(
                f"operator 'between' requires a 2-element list, got {value!r}"
            )
    elif op in ("within", "not_within"):
        if not isinstance(value, str):
            raise ValueError(
                f"operator {op!r} requires a duration string, got {value!r}"
            )
        # Validate the duration is parseable.
        _parse_day_duration(value)
    elif op in ("before", "after"):
        if not isinstance(value, str):
            raise ValueError(f"operator {op!r} requires an ISO-8601 duration string, got {value!r}")
        try:
            parse_iso8601_duration(value)
        except InvalidDurationError as exc:
            raise ValueError(f"invalid duration for field {field!r}: {exc}") from exc

    return {"field": field, op: value}


def _normalize_clause(clause: Any) -> dict:
    """Validate and normalize a single clause (v2 key-op, v3 field/op/value, or nested boolean)."""
    if not isinstance(clause, dict):
        raise ValueError(f"segment clause must be an object, got {clause!r}")

    # Nested boolean node.
    if "all" in clause or "any" in clause or "none" in clause:
        return _normalize_boolean(clause)

    # Legacy kind-based clause.
    if "kind" in clause:
        if clause.get("kind") != "no_engagement_since":
            raise ValueError(f"unknown clause kind: {clause.get('kind')!r}")
        if "window" not in clause or not isinstance(clause["window"], str):
            raise ValueError("no_engagement_since clause requires a string 'window'")
        extra = set(clause) - {"kind", "window"}
        if extra:
            raise ValueError(f"unexpected keys in no_engagement_since clause: {sorted(extra)}")
        try:
            delta = parse_iso8601_duration(clause["window"])
        except InvalidDurationError as exc:
            raise ValueError(f"invalid duration in no_engagement_since clause: {exc}") from exc
        warnings.warn(
            "no_engagement_since clause is deprecated; use "
            "{'field': 'last_open_at', 'op': 'not_within', 'value': '<Nd>'} instead.",
            DeprecationWarning,
            stacklevel=4,
        )
        day_value = f"{int(delta.total_seconds() // 86400)}d"
        return {"field": "last_open_at", "not_within": day_value}

    if "field" not in clause:
        raise ValueError(f"segment clause missing 'field' or 'kind': {clause!r}")

    field = clause["field"]
    if not isinstance(field, str) or not field:
        raise ValueError(f"segment clause 'field' must be a non-empty string, got {field!r}")

    # v3 style: {field, op, value} or {field, op} (for null-check ops).
    if "op" in clause:
        return _normalize_v3_clause(clause)

    # v1/v2 key-operator style: {field, <op>: <value>}.
    unknown_keys = set(clause) - _FIELD_CLAUSE_KEYS
    if unknown_keys:
        raise ValueError(f"unknown keys in segment clause: {sorted(unknown_keys)}")

    ops_present = [op for op in _KEY_OPERATORS if op in clause]
    if len(ops_present) != 1:
        raise ValueError(
            f"segment clause for field {field!r} must specify exactly one operator "
            f"of {_KEY_OPERATORS}, got {ops_present}"
        )
    op = ops_present[0]
    value = clause[op]

    if op in ("in", "not_in"):
        if not isinstance(value, list):
            raise ValueError(f"operator {op!r} requires a list value, got {value!r}")
    elif op in ("before", "after"):
        if not isinstance(value, str):
            raise ValueError(f"operator {op!r} requires an ISO-8601 duration string, got {value!r}")
        try:
            parse_iso8601_duration(value)
        except InvalidDurationError as exc:
            raise ValueError(f"invalid duration for field {field!r}: {exc}") from exc

    return {"field": field, op: value}


def _normalize_boolean(segment: dict) -> dict:
    """Validate and normalize a top-level (or nested) ``all``/``any``/``none`` node."""
    keys = set(segment) & {"all", "any", "none"}
    if len(keys) != 1:
        raise ValueError(
            f"boolean segment node must have exactly one of 'all'/'any'/'none', "
            f"got {sorted(segment)}"
        )
    other_keys = set(segment) - keys
    if other_keys:
        raise ValueError(f"unexpected keys alongside boolean operator: {sorted(other_keys)}")

    combinator = next(iter(keys))
    clauses = segment[combinator]
    if not isinstance(clauses, list):
        raise ValueError(f"{combinator!r} must be a list of clauses, got {clauses!r}")

    return {combinator: [_normalize_clause(c) for c in clauses]}


def _normalize_flat_v1(segment: dict) -> dict:
    """Convert a v1 flat dict (``{"vertical": "auto", ...}``) to v2 form.

    Preserves v1 semantics exactly: a scalar value becomes ``eq``, a list
    value becomes ``in``. An unrecognized top-level key (anything other than
    "vertical" or "attributes.<key>") is preserved as-is on a best-effort
    basis so that ``matches()`` can still fail closed on it — see
    ``test_unknown_key_fails_closed``, which expects ``matches()`` to return
    False rather than raise for ``{"foo": "bar"}``. To keep that test green,
    flat-form normalization does not validate the key shape; it only
    validates that the segment is a dict of scalars/lists, which it already
    is by construction here.
    """
    clauses: list[dict] = []
    for key, expected in segment.items():
        clause: dict[str, Any] = {"field": key}
        if isinstance(expected, list):
            clause["in"] = expected
        else:
            clause["eq"] = expected
        clauses.append(clause)
    return {"all": clauses}


def normalize(segment: dict | None) -> dict:
    """Canonicalize a segment (``None``, v1 flat dict, or v2/v3 dict) to canonical form.

    Args:
        segment: ``None``/``{}`` (matches everything), a v1 flat dict, or an
            already-v2/v3 dict (``{"all": [...]}`` / ``{"any": [...]}`` /
            ``{"none": [...]}``).

    Returns:
        The canonical dict, always shaped ``{"all": [...]}``, ``{"any": [...]}``,
        or ``{"none": [...]}``.

    Raises:
        ValueError: If ``segment`` is structurally invalid (e.g. a
            combinator value that isn't a list, a clause missing both
            "field" and "kind", an unknown operator key, a clause with
            more than one operator, or invalid duration strings). Flat v1
            dicts are intentionally *not* raised on for unrecognized keys,
            to preserve the existing fail-closed-at-match-time behavior of
            v1 (see ``test_unknown_key_fails_closed``).
    """
    if segment is None:
        return {"all": []}
    if not isinstance(segment, dict):
        raise ValueError(f"segment must be a dict or None, got {segment!r}")
    if not segment:
        return {"all": []}

    if "all" in segment or "any" in segment or "none" in segment:
        return _normalize_boolean(segment)

    return _normalize_flat_v1(segment)


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------

_UNKNOWN_FIELD = object()


def _field_value(
    field: str,
    *,
    recipient_attrs: dict,
    vertical: str | None,
    engagement_tier: str | None,
    last_seen_at: datetime | None,
    extra_kwargs: dict[str, Any],
) -> Any:
    """Resolve a field name to its value from the available recipient context.

    Tries, in order: contact_profile kwargs, legacy fields, attribute bag.
    Returns ``_UNKNOWN_FIELD`` sentinel for fields that are not present in
    any lookup location.
    """
    # Contact-profile fields passed as extra kwargs to matches().
    if field in extra_kwargs:
        return extra_kwargs[field]

    # Legacy fields supported by v1/v2.
    if field == "vertical":
        return vertical
    if field == "engagement_tier":
        return engagement_tier
    if field == "last_seen_at":
        return last_seen_at
    if field.startswith("attributes."):
        attr_key = field.split(".", 1)[1]
        return recipient_attrs.get(attr_key) if isinstance(recipient_attrs, dict) else None
    return _UNKNOWN_FIELD


def _eval_clause(
    clause: dict,
    *,
    recipient_attrs: dict,
    vertical: str | None,
    engagement_tier: str | None,
    last_seen_at: datetime | None,
    last_engagement_at: datetime | None,
    now: datetime,
    extra_kwargs: dict[str, Any],
) -> bool:
    """Recursively evaluate a normalized clause against recipient context."""
    if "all" in clause:
        return all(
            _eval_clause(
                c,
                recipient_attrs=recipient_attrs,
                vertical=vertical,
                engagement_tier=engagement_tier,
                last_seen_at=last_seen_at,
                last_engagement_at=last_engagement_at,
                now=now,
                extra_kwargs=extra_kwargs,
            )
            for c in clause["all"]
        )
    if "any" in clause:
        return any(
            _eval_clause(
                c,
                recipient_attrs=recipient_attrs,
                vertical=vertical,
                engagement_tier=engagement_tier,
                last_seen_at=last_seen_at,
                last_engagement_at=last_engagement_at,
                now=now,
                extra_kwargs=extra_kwargs,
            )
            for c in clause["any"]
        )
    if "none" in clause:
        return not any(
            _eval_clause(
                c,
                recipient_attrs=recipient_attrs,
                vertical=vertical,
                engagement_tier=engagement_tier,
                last_seen_at=last_seen_at,
                last_engagement_at=last_engagement_at,
                now=now,
                extra_kwargs=extra_kwargs,
            )
            for c in clause["none"]
        )
    field = clause.get("field")
    if field is None:
        # Unrecognized flat-v1-carried-through clause shape -> fail closed.
        return False

    actual = _field_value(
        field,
        recipient_attrs=recipient_attrs,
        vertical=vertical,
        engagement_tier=engagement_tier,
        last_seen_at=last_seen_at,
        extra_kwargs=extra_kwargs,
    )
    if actual is _UNKNOWN_FIELD:
        return False

    # --- Equality / set operators ---
    if "eq" in clause:
        return actual == clause["eq"]
    if "ne" in clause:
        return actual != clause["ne"]
    if "in" in clause:
        return actual in clause["in"]
    if "not_in" in clause:
        return actual not in clause["not_in"]

    # --- Null-check operators (stored as {op: True} internally) ---
    if "is_null" in clause:
        return actual is None
    if "is_not_null" in clause:
        return actual is not None

    # --- Numeric / datetime comparison operators ---
    if "gt" in clause:
        return actual is not None and actual > clause["gt"]
    if "gte" in clause:
        return actual is not None and actual >= clause["gte"]
    if "lt" in clause:
        return actual is not None and actual < clause["lt"]
    if "lte" in clause:
        return actual is not None and actual <= clause["lte"]

    # --- Range operator ---
    if "between" in clause:
        low, high = clause["between"]
        return actual is not None and low <= actual <= high

    # --- Legacy before/after (ISO-8601 duration from now) ---
    if "before" in clause or "after" in clause:
        if actual is None:
            return False
        if "before" in clause:
            window = parse_iso8601_duration(clause["before"])
            return actual < (now - window)
        window = parse_iso8601_duration(clause["after"])
        return actual > (now - window)

    # --- Timestamp window operators (simple Nd or ISO-8601 duration) ---
    if "within" in clause:
        if actual is None:
            return False
        delta = _parse_day_duration(clause["within"])
        return actual >= (now - delta)
    if "not_within" in clause:
        if actual is None:
            return True
        delta = _parse_day_duration(clause["not_within"])
        return actual < (now - delta)

    # No recognized operator -> fail closed.
    return False


def matches(
    segment: dict | None,
    recipient_attrs: dict,
    vertical: str | None = None,
    *,
    engagement_tier: str | None = None,
    last_seen_at: datetime | None = None,
    last_engagement_at: datetime | None = None,
    now: datetime | None = None,
    **contact_profile_kwargs: Any,
) -> bool:
    """Check whether a recipient matches a segment (v1 flat, v2, or v3 DSL).

    Backward compatible with the v1 positional signature
    (``matches(segment, attrs, vertical)``); v2/v3 fields are opt-in keyword
    args used only by clauses that reference them.

    Args:
        segment: ``None``/``{}`` (matches everything), a v1 flat dict, or a
            v2/v3 ``{"all"/"any"/"none": [...]}`` dict.
        recipient_attrs: The recipient's ``attributes`` JSON blob.
        vertical: The recipient's vertical, for ``"vertical"`` field clauses.
        engagement_tier: The recipient's engagement tier, for
            ``"engagement_tier"`` field clauses.
        last_seen_at: The recipient's last-seen timestamp, for
            ``"last_seen_at"`` before/after clauses.
        last_engagement_at: Retained for backward compatibility with v1/v2
            callers that pass this directly. Post-normalization, the
            ``no_engagement_since`` kind no longer exists in canonical form;
            the equivalent ``not_within`` clause on ``last_open_at`` is used
            instead, resolved via ``contact_profile_kwargs``.
        now: Reference time for relative-duration comparisons; defaults to
            the current UTC time. Exposed for deterministic testing.
        **contact_profile_kwargs: Any additional keyword arguments are
            treated as contact_profile field values (e.g.
            ``last_open_at=...``, ``complaints_90d=2``).

    Returns:
        True iff the recipient matches; False otherwise (including on any
        field/clause the evaluator doesn't recognize — fail closed).
    """
    try:
        normalized = normalize(segment)
    except ValueError:
        return False

    resolved_now = now if now is not None else datetime.now(timezone.utc)
    return _eval_clause(
        normalized,
        recipient_attrs=recipient_attrs,
        vertical=vertical,
        engagement_tier=engagement_tier,
        last_seen_at=last_seen_at,
        last_engagement_at=last_engagement_at,
        now=resolved_now,
        extra_kwargs=contact_profile_kwargs,
    )
