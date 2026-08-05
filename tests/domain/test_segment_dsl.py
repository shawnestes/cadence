"""Pure unit tests for segment DSL v3 operators and combinators.

Covers scenarios 15, 15a, 15e from SPEC-segmentation.md plus the full set of
v3 operators: within, not_within, ne, gt, lte, is_null, is_not_null, between,
and the none combinator.

No DB, no fixtures.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.segment import matches, normalize

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
_25_DAYS_AGO = _NOW - timedelta(days=25)
_35_DAYS_AGO = _NOW - timedelta(days=35)


# ---------------------------------------------------------------------------
# within operator
# ---------------------------------------------------------------------------


def test_within_true_when_inside_window():
    """last_open_at 25 days ago is within 30d → True."""
    seg = {"all": [{"field": "last_open_at", "op": "within", "value": "30d"}]}
    assert matches(seg, {}, last_open_at=_25_DAYS_AGO, now=_NOW) is True


def test_within_false_when_outside_window():
    """last_open_at 35 days ago is NOT within 30d → False."""
    seg = {"all": [{"field": "last_open_at", "op": "within", "value": "30d"}]}
    assert matches(seg, {}, last_open_at=_35_DAYS_AGO, now=_NOW) is False


def test_within_false_when_field_is_none():
    """last_open_at=None (never opened) is NOT within any window → False (fail-closed)."""
    seg = {"all": [{"field": "last_open_at", "op": "within", "value": "30d"}]}
    assert matches(seg, {}, last_open_at=None, now=_NOW) is False


# ---------------------------------------------------------------------------
# not_within operator
# ---------------------------------------------------------------------------


def test_not_within_true_when_outside_window():
    """35 days ago is NOT within 30d → not_within returns True."""
    seg = {"all": [{"field": "last_open_at", "op": "not_within", "value": "30d"}]}
    assert matches(seg, {}, last_open_at=_35_DAYS_AGO, now=_NOW) is True


def test_not_within_false_when_inside_window():
    """25 days ago IS within 30d → not_within returns False."""
    seg = {"all": [{"field": "last_open_at", "op": "not_within", "value": "30d"}]}
    assert matches(seg, {}, last_open_at=_25_DAYS_AGO, now=_NOW) is False


def test_not_within_true_when_field_is_none():
    """last_open_at=None means never opened = not within any window → True."""
    seg = {"all": [{"field": "last_open_at", "op": "not_within", "value": "30d"}]}
    assert matches(seg, {}, last_open_at=None, now=_NOW) is True


# ---------------------------------------------------------------------------
# none combinator
# ---------------------------------------------------------------------------


def test_none_combinator_true_when_no_clause_matches():
    """none: [...] is True when none of the clauses match (complaints_90d=0 < 1)."""
    seg = {"none": [{"field": "complaints_90d", "op": "gte", "value": 1}]}
    assert matches(seg, {}, complaints_90d=0, now=_NOW) is True


def test_none_combinator_false_when_a_clause_matches():
    """none: [...] is False when at least one clause matches (complaints_90d=2 >= 1)."""
    seg = {"none": [{"field": "complaints_90d", "op": "gte", "value": 1}]}
    assert matches(seg, {}, complaints_90d=2, now=_NOW) is False


def test_none_combinator_empty_list_is_true():
    """none: [] — no clauses to fail → always True."""
    seg = {"none": []}
    assert matches(seg, {}) is True


# ---------------------------------------------------------------------------
# ne operator
# ---------------------------------------------------------------------------


def test_ne_true_when_values_differ():
    seg = {"all": [{"field": "engagement_tier", "op": "ne", "value": "cold"}]}
    assert matches(seg, {}, engagement_tier="active") is True


def test_ne_false_when_values_equal():
    seg = {"all": [{"field": "engagement_tier", "op": "ne", "value": "cold"}]}
    assert matches(seg, {}, engagement_tier="cold") is False


# ---------------------------------------------------------------------------
# gt operator
# ---------------------------------------------------------------------------


def test_gt_true_when_greater():
    seg = {"all": [{"field": "sends_30d", "op": "gt", "value": 5}]}
    assert matches(seg, {}, sends_30d=10) is True


def test_gt_false_when_equal():
    seg = {"all": [{"field": "sends_30d", "op": "gt", "value": 5}]}
    assert matches(seg, {}, sends_30d=5) is False


def test_gt_false_when_less():
    seg = {"all": [{"field": "sends_30d", "op": "gt", "value": 5}]}
    assert matches(seg, {}, sends_30d=3) is False


# ---------------------------------------------------------------------------
# lte operator
# ---------------------------------------------------------------------------


def test_lte_true_when_equal():
    seg = {"all": [{"field": "sends_30d", "op": "lte", "value": 2}]}
    assert matches(seg, {}, sends_30d=2) is True


def test_lte_true_when_less():
    seg = {"all": [{"field": "sends_30d", "op": "lte", "value": 2}]}
    assert matches(seg, {}, sends_30d=1) is True


def test_lte_false_when_greater():
    seg = {"all": [{"field": "sends_30d", "op": "lte", "value": 2}]}
    assert matches(seg, {}, sends_30d=3) is False


# ---------------------------------------------------------------------------
# is_null operator
# ---------------------------------------------------------------------------


def test_is_null_true_when_none():
    seg = {"all": [{"field": "last_open_at", "op": "is_null"}]}
    assert matches(seg, {}, last_open_at=None) is True


def test_is_null_false_when_not_none():
    seg = {"all": [{"field": "last_open_at", "op": "is_null"}]}
    assert matches(seg, {}, last_open_at=_25_DAYS_AGO) is False


# ---------------------------------------------------------------------------
# is_not_null operator
# ---------------------------------------------------------------------------


def test_is_not_null_true_when_value_present():
    seg = {"all": [{"field": "last_open_at", "op": "is_not_null"}]}
    assert matches(seg, {}, last_open_at=_25_DAYS_AGO) is True


def test_is_not_null_false_when_none():
    seg = {"all": [{"field": "last_open_at", "op": "is_not_null"}]}
    assert matches(seg, {}, last_open_at=None) is False


# ---------------------------------------------------------------------------
# between operator
# ---------------------------------------------------------------------------


def test_between_true_when_inside_range():
    seg = {"all": [{"field": "sends_30d", "op": "between", "value": [1, 5]}]}
    assert matches(seg, {}, sends_30d=3) is True


def test_between_true_at_lower_bound():
    seg = {"all": [{"field": "sends_30d", "op": "between", "value": [1, 5]}]}
    assert matches(seg, {}, sends_30d=1) is True


def test_between_true_at_upper_bound():
    seg = {"all": [{"field": "sends_30d", "op": "between", "value": [1, 5]}]}
    assert matches(seg, {}, sends_30d=5) is True


def test_between_false_below_range():
    seg = {"all": [{"field": "sends_30d", "op": "between", "value": [1, 5]}]}
    assert matches(seg, {}, sends_30d=0) is False


def test_between_false_above_range():
    seg = {"all": [{"field": "sends_30d", "op": "between", "value": [1, 5]}]}
    assert matches(seg, {}, sends_30d=6) is False


def test_between_false_when_field_is_none():
    seg = {"all": [{"field": "sends_30d", "op": "between", "value": [1, 5]}]}
    assert matches(seg, {}, sends_30d=None) is False


# ---------------------------------------------------------------------------
# Nested all + none — cable_dormant_reengage predicate shape
# ---------------------------------------------------------------------------
#
# Semantic equivalent of segments/email/cable_dormant.yaml:
#   mailbox_provider = cable AND engagement_tier = dormant
#   AND sends_30d <= 2 AND NOT (complaints_90d >= 1)
#
# Note: the DSL supports only ONE top-level combinator key (all/any/none).
# A combined all+none at the top level is rejected by normalize().
# The correct representation nests the none clause inside the all list
# as a nested boolean node.


_CABLE_DORMANT_SEG = {
    "all": [
        {"field": "mailbox_provider", "op": "eq", "value": "cable"},
        {"field": "engagement_tier", "op": "eq", "value": "dormant"},
        {"field": "sends_30d", "op": "lte", "value": 2},
        {"none": [{"field": "complaints_90d", "op": "gte", "value": 1}]},
    ]
}


def test_cable_dormant_match_no_complaints():
    """Contact with cable + dormant + sends_30d=1 + complaints_90d=0 matches."""
    assert matches(
        _CABLE_DORMANT_SEG,
        {},
        mailbox_provider="cable",
        engagement_tier="dormant",
        sends_30d=1,
        complaints_90d=0,
        now=_NOW,
    ) is True


def test_cable_dormant_no_match_has_complaint():
    """Same contact with complaints_90d=1 does NOT match (none combinator excludes)."""
    assert matches(
        _CABLE_DORMANT_SEG,
        {},
        mailbox_provider="cable",
        engagement_tier="dormant",
        sends_30d=1,
        complaints_90d=1,
        now=_NOW,
    ) is False


def test_cable_dormant_no_match_wrong_provider():
    """Gmail contact is excluded by mailbox_provider=cable clause."""
    assert matches(
        _CABLE_DORMANT_SEG,
        {},
        mailbox_provider="gmail",
        engagement_tier="dormant",
        sends_30d=1,
        complaints_90d=0,
        now=_NOW,
    ) is False


def test_cable_dormant_no_match_too_many_sends():
    """sends_30d=3 exceeds lte:2 → no match."""
    assert matches(
        _CABLE_DORMANT_SEG,
        {},
        mailbox_provider="cable",
        engagement_tier="dormant",
        sends_30d=3,
        complaints_90d=0,
        now=_NOW,
    ) is False


# ---------------------------------------------------------------------------
# Scenario 15 — segment without acknowledges_cold excludes cold contacts
#               at SQL-compile time (the guard is in compile_segment, not in
#               matches()).  Here we test that the none combinator path itself
#               is exercised and that matches() alone does NOT enforce the cold
#               exclusion — that is the compiler's job.
# ---------------------------------------------------------------------------


def test_scenario_15_none_combinator_exercised_for_cold_exclusion():
    """Scenario 15: a segment with a nested none clause correctly excludes a
    cold contact via the none combinator, exercising that code path in _eval_clause.

    The none clause must be nested inside the all list (the DSL allows only one
    top-level combinator key). This mirrors what compile_segment injects for
    segments without acknowledges_cold=True.
    """
    seg = {
        "all": [
            {"field": "mailbox_provider", "op": "eq", "value": "yahoo"},
            {"none": [{"field": "engagement_tier", "op": "eq", "value": "cold"}]},
        ]
    }
    # Non-cold contact matches.
    assert matches(seg, {}, mailbox_provider="yahoo", engagement_tier="active") is True
    # Cold contact excluded by none combinator.
    assert matches(seg, {}, mailbox_provider="yahoo", engagement_tier="cold") is False


# ---------------------------------------------------------------------------
# Scenario 15a — acknowledges_cold guard is in the SQL compiler, not in
#                matches().  A segment whose predicate targets cold contacts
#                directly will return True from matches() because matches()
#                evaluates the predicate as written; the cold-exclusion guard
#                is injected by compile_segment() at SQL compile time, not here.
# ---------------------------------------------------------------------------


def test_scenario_15a_matches_does_not_enforce_cold_guard():
    """Scenario 15a: matches() itself does NOT enforce the acknowledges_cold guard.

    The guard is a SQL-compile-time concern in compile_segment().  A segment
    predicate that directly targets engagement_tier='cold' will return True
    from matches() when given a cold contact — because matches() evaluates the
    predicate as written, without any acknowledges_cold override.

    The comment here documents this intentional design: if a YAML segment has
    acknowledges_cold: false (the default), compile_segment() adds a
    "AND engagement_tier != cold" guard to the SQL, but matches() has no such
    concept.  Tests of the SQL guard belong in test_segment_sql.py / the
    segment preview API tests.
    """
    seg = {"all": [{"field": "engagement_tier", "op": "eq", "value": "cold"}]}
    # matches() returns True for cold contacts because it evaluates the predicate
    # as written — the compile_segment guard is applied separately.
    assert matches(seg, {}, engagement_tier="cold") is True


# ---------------------------------------------------------------------------
# Scenario 15e — within operator with different time values
# ---------------------------------------------------------------------------


def test_scenario_15e_within_boundary_exact():
    """Scenario 15e: contact opened exactly 30 days ago is within 30d."""
    exactly_30_ago = _NOW - timedelta(days=30)
    seg = {"all": [{"field": "last_open_at", "op": "within", "value": "30d"}]}
    # >= boundary: exactly 30 days ago qualifies (now - 30d == threshold)
    assert matches(seg, {}, last_open_at=exactly_30_ago, now=_NOW) is True


def test_scenario_15e_within_one_day_before_boundary():
    """Scenario 15e: contact opened 31 days ago is NOT within 30d."""
    thirty_one_ago = _NOW - timedelta(days=31)
    seg = {"all": [{"field": "last_open_at", "op": "within", "value": "30d"}]}
    assert matches(seg, {}, last_open_at=thirty_one_ago, now=_NOW) is False


def test_scenario_15e_within_iso8601_duration():
    """Scenario 15e: within operator also accepts ISO-8601 duration strings like P30D."""
    seg = {"all": [{"field": "last_open_at", "op": "within", "value": "P30D"}]}
    assert matches(seg, {}, last_open_at=_25_DAYS_AGO, now=_NOW) is True
    assert matches(seg, {}, last_open_at=_35_DAYS_AGO, now=_NOW) is False


def test_scenario_15e_within_90d_window():
    """Scenario 15e: wider 90d window — 35-day-old open qualifies."""
    seg = {"all": [{"field": "last_open_at", "op": "within", "value": "90d"}]}
    assert matches(seg, {}, last_open_at=_35_DAYS_AGO, now=_NOW) is True


def test_scenario_15e_not_within_90d_when_120_days_ago():
    """Scenario 15e: not_within 90d — 120-day-old value is outside the window."""
    old = _NOW - timedelta(days=120)
    seg = {"all": [{"field": "last_open_at", "op": "not_within", "value": "90d"}]}
    assert matches(seg, {}, last_open_at=old, now=_NOW) is True


# ---------------------------------------------------------------------------
# normalize() round-trip for new v3 operators
# ---------------------------------------------------------------------------


def test_normalize_v3_within_clause():
    result = normalize({"all": [{"field": "last_open_at", "op": "within", "value": "30d"}]})
    assert result == {"all": [{"field": "last_open_at", "within": "30d"}]}


def test_normalize_v3_not_within_clause():
    result = normalize({"all": [{"field": "last_open_at", "op": "not_within", "value": "30d"}]})
    assert result == {"all": [{"field": "last_open_at", "not_within": "30d"}]}


def test_normalize_v3_none_combinator():
    seg = {"none": [{"field": "complaints_90d", "op": "gte", "value": 1}]}
    result = normalize(seg)
    assert result == {"none": [{"field": "complaints_90d", "gte": 1}]}


def test_normalize_v3_between_clause():
    seg = {"all": [{"field": "sends_30d", "op": "between", "value": [1, 5]}]}
    result = normalize(seg)
    assert result == {"all": [{"field": "sends_30d", "between": [1, 5]}]}


def test_normalize_v3_is_null():
    seg = {"all": [{"field": "last_open_at", "op": "is_null"}]}
    result = normalize(seg)
    assert result == {"all": [{"field": "last_open_at", "is_null": True}]}


def test_contains_matches_list_valued_attribute():
    seg = {"all": [{"field": "attributes.vehicle_makes", "op": "contains", "value": "toyota"}]}
    assert matches(seg, {"vehicle_makes": ["toyota", "honda"]}, "auto") is True
    assert matches(seg, {"vehicle_makes": ["ford"]}, "auto") is False


def test_contains_on_scalar_attribute_is_false_not_an_error():
    seg = {"all": [{"field": "attributes.vehicle_makes", "op": "contains", "value": "toyota"}]}
    assert matches(seg, {"vehicle_makes": "toyota"}, "auto") is False


def test_contains_on_missing_attribute_is_false():
    seg = {"all": [{"field": "attributes.vehicle_makes", "op": "contains", "value": "toyota"}]}
    assert matches(seg, {}, "auto") is False


def test_normalize_accepts_contains():
    assert normalize(
        {"all": [{"field": "attributes.vehicle_makes", "op": "contains", "value": "toyota"}]}
    ) == {"all": [{"field": "attributes.vehicle_makes", "contains": "toyota"}]}


def test_is_null_true_when_attribute_key_absent():
    seg = {"all": [{"field": "attributes.phone", "op": "is_null"}]}
    assert matches(seg, {}, "auto") is True


def test_is_null_true_when_attribute_present_but_explicit_none():
    seg = {"all": [{"field": "attributes.phone", "op": "is_null"}]}
    assert matches(seg, {"phone": None}, "auto") is True


def test_is_null_false_when_attribute_has_a_value():
    seg = {"all": [{"field": "attributes.phone", "op": "is_null"}]}
    assert matches(seg, {"phone": "555-1234"}, "auto") is False


def test_is_not_null_false_when_attribute_key_absent():
    seg = {"all": [{"field": "attributes.phone", "op": "is_not_null"}]}
    assert matches(seg, {}, "auto") is False


def test_is_not_null_false_when_attribute_present_but_explicit_none():
    seg = {"all": [{"field": "attributes.phone", "op": "is_not_null"}]}
    assert matches(seg, {"phone": None}, "auto") is False


def test_is_not_null_true_when_attribute_has_a_value():
    seg = {"all": [{"field": "attributes.phone", "op": "is_not_null"}]}
    assert matches(seg, {"phone": "555-1234"}, "auto") is True
