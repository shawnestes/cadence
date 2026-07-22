"""Pure tests for the segment DSL: v1 flat-dict matcher + v2 normalize()."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.segment import matches, normalize


def test_null_segment_matches_all():
    assert matches(None, {}) is True
    assert matches({}, {}) is True


def test_vertical_filter():
    assert matches({"vertical": "auto"}, {}, vertical="auto") is True
    assert matches({"vertical": "auto"}, {}, vertical="home") is False


def test_attribute_filter():
    assert matches({"attributes.state": "TX"}, {"state": "TX"}) is True
    assert matches({"attributes.state": "TX"}, {"state": "CA"}) is False


def test_list_membership():
    seg = {"attributes.age_band": ["35-44", "45-54"]}
    assert matches(seg, {"age_band": "35-44"}) is True
    assert matches(seg, {"age_band": "25-34"}) is False


def test_unknown_key_fails_closed():
    assert matches({"foo": "bar"}, {}) is False


# --- normalize() -----------------------------------------------------------


def test_normalize_none_and_empty_matches_all():
    assert normalize(None) == {"all": []}
    assert normalize({}) == {"all": []}


def test_normalize_flat_v1_scalar_becomes_eq():
    assert normalize({"vertical": "auto"}) == {
        "all": [{"field": "vertical", "eq": "auto"}]
    }


def test_normalize_flat_v1_list_becomes_in():
    assert normalize({"attributes.age_band": ["35-44", "45-54"]}) == {
        "all": [{"field": "attributes.age_band", "in": ["35-44", "45-54"]}]
    }


def test_normalize_flat_v1_multi_key_preserves_order_as_all():
    result = normalize({"vertical": "auto", "attributes.state": "TX"})
    assert result == {
        "all": [
            {"field": "vertical", "eq": "auto"},
            {"field": "attributes.state", "eq": "TX"},
        ]
    }


def test_normalize_v2_all_passthrough():
    seg = {"all": [{"field": "vertical", "eq": "auto"}]}
    assert normalize(seg) == seg


def test_normalize_v2_any_passthrough():
    seg = {"any": [{"field": "vertical", "eq": "auto"}, {"field": "vertical", "eq": "home"}]}
    assert normalize(seg) == seg


def test_normalize_v2_nested_any_within_all():
    seg = {
        "all": [
            {"field": "vertical", "eq": "auto"},
            {"any": [{"field": "attributes.state", "eq": "TX"}, {"field": "attributes.state", "eq": "OK"}]},
        ]
    }
    assert normalize(seg) == seg


def test_normalize_v2_no_engagement_since_clause():
    seg = {"all": [{"kind": "no_engagement_since", "window": "P60D"}]}
    assert normalize(seg) == seg


@pytest.mark.parametrize(
    "bad_segment",
    [
        {"all": "not-a-list"},
        {"any": {"field": "vertical", "eq": "auto"}},
        {"all": [{}]},
        {"all": [{"field": "vertical", "bogus_op": "auto"}]},
        {"all": [{"field": "vertical", "eq": "auto", "in": ["auto"]}]},
        {"all": [{"field": "vertical", "in": "not-a-list"}]},
        {"all": [{"field": "vertical", "before": "not-a-duration"}]},
        {"all": [{"kind": "no_engagement_since"}]},
        {"all": [{"kind": "unknown_kind", "window": "P1D"}]},
        {"all": [{"all": [{"field": "vertical", "eq": "auto"}], "any": []}]},
        "not-a-dict",
        123,
    ],
)
def test_normalize_raises_on_garbage(bad_segment):
    with pytest.raises(ValueError):
        normalize(bad_segment)


# --- matches() v2 operators --------------------------------------------------


def test_matches_v2_eq_and_in_not_in():
    seg = {
        "all": [
            {"field": "vertical", "eq": "auto"},
            {"field": "attributes.state", "in": ["TX", "OK"]},
            {"field": "attributes.age_band", "not_in": ["18-24"]},
        ]
    }
    assert matches(seg, {"state": "TX", "age_band": "35-44"}, vertical="auto") is True
    assert matches(seg, {"state": "CA", "age_band": "35-44"}, vertical="auto") is False
    assert matches(seg, {"state": "TX", "age_band": "18-24"}, vertical="auto") is False


def test_matches_v2_engagement_tier_field():
    seg = {"all": [{"field": "engagement_tier", "eq": "neutral"}]}
    assert matches(seg, {}, engagement_tier="neutral") is True
    assert matches(seg, {}, engagement_tier="hot") is False


def test_matches_v2_before_after_last_seen_at():
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    seg_before = {"all": [{"field": "last_seen_at", "before": "P60D"}]}
    seg_after = {"all": [{"field": "last_seen_at", "after": "P60D"}]}

    old = now - timedelta(days=90)
    recent = now - timedelta(days=10)

    assert matches(seg_before, {}, last_seen_at=old, now=now) is True
    assert matches(seg_before, {}, last_seen_at=recent, now=now) is False
    assert matches(seg_after, {}, last_seen_at=recent, now=now) is True
    assert matches(seg_after, {}, last_seen_at=old, now=now) is False


def test_matches_v2_before_missing_field_value_fails_closed():
    seg = {"all": [{"field": "last_seen_at", "before": "P60D"}]}
    assert matches(seg, {}, last_seen_at=None) is False


def test_matches_v2_no_engagement_since():
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    seg = {"all": [{"kind": "no_engagement_since", "window": "P60D"}]}

    assert matches(seg, {}, last_engagement_at=None, now=now) is True
    assert (
        matches(seg, {}, last_engagement_at=now - timedelta(days=90), now=now) is True
    )
    assert (
        matches(seg, {}, last_engagement_at=now - timedelta(days=10), now=now) is False
    )


def test_matches_v2_nested_all_any():
    seg = {
        "all": [
            {"field": "vertical", "eq": "auto"},
            {
                "any": [
                    {"field": "attributes.state", "eq": "TX"},
                    {"field": "attributes.state", "eq": "OK"},
                ]
            },
        ]
    }
    assert matches(seg, {"state": "TX"}, vertical="auto") is True
    assert matches(seg, {"state": "OK"}, vertical="auto") is True
    assert matches(seg, {"state": "CA"}, vertical="auto") is False
    assert matches(seg, {"state": "TX"}, vertical="home") is False


def test_matches_v2_deeply_nested_any_within_all_within_any():
    seg = {
        "any": [
            {
                "all": [
                    {"field": "vertical", "eq": "auto"},
                    {"any": [{"field": "attributes.state", "eq": "TX"}]},
                ]
            },
            {"field": "vertical", "eq": "home"},
        ]
    }
    assert matches(seg, {"state": "TX"}, vertical="auto") is True
    assert matches(seg, {"state": "CA"}, vertical="auto") is False
    assert matches(seg, {}, vertical="home") is True


def test_matches_backward_compat_positional_signature():
    # v1 callers pass (segment, attrs, vertical) positionally.
    assert matches({"vertical": "auto"}, {"state": "TX"}, "auto") is True
    assert matches({"vertical": "auto"}, {"state": "TX"}, "home") is False
