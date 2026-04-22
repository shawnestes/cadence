"""Pure tests for the segment matcher."""

from __future__ import annotations

from app.domain.segment import matches


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
