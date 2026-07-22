"""Tests for app.services.segment_query.compile_segment.

The compiler itself has no DB requirement to *build* a SQLAlchemy
expression, but the fastest reliable way to validate it produces correct
results (JSONB ->>'key' extraction, NOT EXISTS subqueries, before/after
duration comparisons) is to execute compiled queries against real seeded
rows — that's covered end-to-end via tests/api/test_segments.py, which
exercises compile_segment through POST /api/segments/preview against the
test Postgres DB.

This module covers what's testable without a DB: that compile_segment
builds a well-formed boolean SQLAlchemy expression (structural checks via
str(compiled_statement)) and that it raises on invalid segments, without
needing app fixtures.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.recipient import Recipient
from app.services.segment_query import compile_segment


def _compiled_sql(segment: dict | None, **kwargs) -> str:
    predicate = compile_segment(segment, **kwargs)
    stmt = select(Recipient).where(predicate)
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_compile_none_segment_matches_all():
    sql = _compiled_sql(None)
    assert "WHERE true" in sql or "WHERE 1 = 1" in sql or sql.strip().endswith(
        "recipients"
    ) or "true" in sql.lower()


def test_compile_flat_v1_vertical_eq():
    sql = _compiled_sql({"vertical": "auto"})
    assert "recipients.vertical" in sql
    assert "'auto'" in sql


def test_compile_v2_attribute_eq_uses_jsonb_extract():
    sql = _compiled_sql({"all": [{"field": "attributes.state", "eq": "TX"}]})
    assert "->>" in sql
    assert "'state'" in sql
    assert "'TX'" in sql


def test_compile_v2_in_and_not_in():
    sql_in = _compiled_sql({"all": [{"field": "attributes.state", "in": ["TX", "OK"]}]})
    assert "IN" in sql_in.upper()

    sql_not_in = _compiled_sql(
        {"all": [{"field": "attributes.age_band", "not_in": ["18-24"]}]}
    )
    assert "NOT IN" in sql_not_in.upper()


def test_compile_v2_engagement_tier_direct_column():
    sql = _compiled_sql({"all": [{"field": "engagement_tier", "eq": "neutral"}]})
    assert "recipients.engagement_tier" in sql


def test_compile_v2_before_after_uses_last_seen_at_column():
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    sql = _compiled_sql(
        {"all": [{"field": "last_seen_at", "before": "P60D"}]}, now=now
    )
    assert "recipients.last_seen_at" in sql
    assert "<" in sql


def test_compile_v2_no_engagement_since_uses_not_exists():
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    sql = _compiled_sql(
        {"all": [{"kind": "no_engagement_since", "window": "P60D"}]}, now=now
    )
    assert "NOT (EXISTS" in sql.upper() or "NOT EXISTS" in sql.upper()
    assert "events" in sql.lower()


def test_compile_v2_any_uses_or():
    sql = _compiled_sql(
        {
            "any": [
                {"field": "vertical", "eq": "auto"},
                {"field": "vertical", "eq": "home"},
            ]
        }
    )
    assert " OR " in sql.upper()


def test_compile_v2_all_uses_and():
    sql = _compiled_sql(
        {
            "all": [
                {"field": "vertical", "eq": "auto"},
                {"field": "attributes.state", "eq": "TX"},
            ]
        }
    )
    assert " AND " in sql.upper()


def test_compile_invalid_segment_raises():
    with pytest.raises(ValueError):
        compile_segment({"all": "not-a-list"})
