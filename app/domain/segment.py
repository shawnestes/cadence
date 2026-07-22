"""Pure segment DSL: normalization and per-recipient evaluation.

v1 supported a tiny flat dict DSL (see below). v2 (SPEC_ADDENDUM_002.md §3.1)
adds explicit boolean nesting (``all``/``any``), comparison operators
(``eq``/``in``/``not_in``/``before``/``after``), and a
``no_engagement_since`` clause kind. ``normalize()`` canonicalizes any
accepted input (``None``, a v1 flat dict, or an already-v2 dict) into the
canonical v2 shape ``{"all": [...]}`` / ``{"any": [...]}``; ``matches()``
evaluates a (normalized-or-not) segment against a single recipient's
attributes.

No FastAPI or SQLAlchemy imports — this module must stay pure per CLAUDE.md
so it is usable both for per-recipient evaluation and (via
``app/services/segment_query.py``) as the source of truth normalized by the
SQL compiler.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain.duration import InvalidDurationError, parse_iso8601_duration

__all__ = ["normalize", "matches"]

_OPERATORS = ("eq", "in", "not_in", "before", "after")
_FIELD_CLAUSE_KEYS = {"field", "eq", "in", "not_in", "before", "after"}


def _normalize_clause(clause: Any) -> dict:
    """Validate and pass through a single v2 clause (field- or kind-based)."""
    if not isinstance(clause, dict):
        raise ValueError(f"segment clause must be an object, got {clause!r}")

    if "all" in clause or "any" in clause:
        return _normalize_boolean(clause)

    if "kind" in clause:
        if clause.get("kind") != "no_engagement_since":
            raise ValueError(f"unknown clause kind: {clause.get('kind')!r}")
        if "window" not in clause or not isinstance(clause["window"], str):
            raise ValueError("no_engagement_since clause requires a string 'window'")
        extra = set(clause) - {"kind", "window"}
        if extra:
            raise ValueError(f"unexpected keys in no_engagement_since clause: {sorted(extra)}")
        try:
            parse_iso8601_duration(clause["window"])
        except InvalidDurationError as exc:
            raise ValueError(f"invalid duration in no_engagement_since clause: {exc}") from exc
        return {"kind": "no_engagement_since", "window": clause["window"]}

    if "field" not in clause:
        raise ValueError(f"segment clause missing 'field' or 'kind': {clause!r}")

    field = clause["field"]
    if not isinstance(field, str) or not field:
        raise ValueError(f"segment clause 'field' must be a non-empty string, got {field!r}")

    unknown_keys = set(clause) - _FIELD_CLAUSE_KEYS
    if unknown_keys:
        raise ValueError(f"unknown keys in segment clause: {sorted(unknown_keys)}")

    ops_present = [op for op in _OPERATORS if op in clause]
    if len(ops_present) != 1:
        raise ValueError(
            f"segment clause for field {field!r} must specify exactly one operator "
            f"of {_OPERATORS}, got {ops_present}"
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
    """Validate and normalize a top-level (or nested) ``all``/``any`` node."""
    keys = set(segment) & {"all", "any"}
    if len(keys) != 1:
        raise ValueError(
            f"boolean segment node must have exactly one of 'all'/'any', got {sorted(segment)}"
        )
    other_keys = set(segment) - keys
    if other_keys:
        raise ValueError(f"unexpected keys alongside boolean operator: {sorted(other_keys)}")

    op = next(iter(keys))
    clauses = segment[op]
    if not isinstance(clauses, list):
        raise ValueError(f"{op!r} must be a list of clauses, got {clauses!r}")

    return {op: [_normalize_clause(c) for c in clauses]}


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
    """Canonicalize a segment (``None``, v1 flat dict, or v2 dict) to v2 form.

    Args:
        segment: ``None``/``{}`` (matches everything), a v1 flat dict, or an
            already-v2 dict (``{"all": [...]}`` / ``{"any": [...]}``).

    Returns:
        The canonical v2 dict, always shaped ``{"all": [...]}`` or
        ``{"any": [...]}``.

    Raises:
        ValueError: If ``segment`` is structurally invalid v2 (e.g. an
            ``all``/``any`` value that isn't a list, a clause missing both
            "field" and "kind", an unknown operator key, or a clause with
            more than one operator). Flat v1 dicts are intentionally *not*
            raised on for unrecognized keys, to preserve the existing
            fail-closed-at-match-time behavior of v1 (see
            ``test_unknown_key_fails_closed``).
    """
    if segment is None:
        return {"all": []}
    if not isinstance(segment, dict):
        raise ValueError(f"segment must be a dict or None, got {segment!r}")
    if not segment:
        return {"all": []}

    if "all" in segment or "any" in segment:
        return _normalize_boolean(segment)

    return _normalize_flat_v1(segment)


def _field_value(
    field: str,
    *,
    recipient_attrs: dict,
    vertical: str | None,
    engagement_tier: str | None,
    last_seen_at: datetime | None,
) -> Any:
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


_UNKNOWN_FIELD = object()


def _eval_clause(
    clause: dict,
    *,
    recipient_attrs: dict,
    vertical: str | None,
    engagement_tier: str | None,
    last_seen_at: datetime | None,
    last_engagement_at: datetime | None,
    now: datetime,
) -> bool:
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
            )
            for c in clause["any"]
        )
    if clause.get("kind") == "no_engagement_since":
        window = parse_iso8601_duration(clause["window"])
        return last_engagement_at is None or last_engagement_at < (now - window)

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
    )
    if actual is _UNKNOWN_FIELD:
        return False

    if "eq" in clause:
        return actual == clause["eq"]
    if "in" in clause:
        return actual in clause["in"]
    if "not_in" in clause:
        return actual not in clause["not_in"]
    if "before" in clause or "after" in clause:
        if actual is None:
            return False
        if "before" in clause:
            window = parse_iso8601_duration(clause["before"])
            return actual < (now - window)
        window = parse_iso8601_duration(clause["after"])
        return actual > (now - window)

    # No recognized operator -> fail closed (covers the v1 "unknown key"
    # passthrough case, where the flat clause is just {"field": "foo"} with
    # no eq/in populated because normalize() didn't recognize the key).
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
) -> bool:
    """Check whether a recipient matches a segment (v1 flat or v2 DSL).

    Backward compatible with the v1 positional signature
    (``matches(segment, attrs, vertical)``); v2 fields are opt-in keyword
    args used only by clauses that reference them.

    Args:
        segment: ``None``/``{}`` (matches everything), a v1 flat dict, or a
            v2 ``{"all"/"any": [...]}`` dict.
        recipient_attrs: The recipient's ``attributes`` JSON blob.
        vertical: The recipient's vertical, for ``"vertical"`` field clauses.
        engagement_tier: The recipient's engagement tier, for
            ``"engagement_tier"`` field clauses.
        last_seen_at: The recipient's last-seen timestamp, for
            ``"last_seen_at"`` before/after clauses.
        last_engagement_at: The recipient's most recent open/click
            timestamp (or ``None`` if they've never engaged), for
            ``no_engagement_since`` clauses.
        now: Reference time for relative-duration comparisons; defaults to
            the current UTC time. Exposed for deterministic testing.

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
    )
