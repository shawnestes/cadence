"""Tests for app.services.segment_query.compile_segment.

The compiler produces parameterized SQL WHERE clauses targeting the
contact_profile table (``cp.*`` column references). This module validates
structural correctness of the generated SQL without a DB connection.

End-to-end correctness against real rows is covered by
tests/api/test_segments.py via POST /api/segments/preview.
"""

from __future__ import annotations

import warnings
from datetime import datetime, timezone

import pytest

from app.core.config import load_engagement_config
from app.services.segment_query import compile_segment

_DEFAULT_CONFIG = load_engagement_config()


def _compiled_sql(segment: dict | None, config=None, **kwargs) -> str:
    effective_config = config if config is not None else _DEFAULT_CONFIG
    where_sql, _params = compile_segment(segment, effective_config, **kwargs)
    return where_sql


def test_compile_none_segment_matches_all():
    sql = _compiled_sql(None, include_unsendable=True, acknowledges_cold=True)
    assert sql == "true"


def test_compile_cp_engagement_tier_eq():
    # engagement_tier is a smallint-coded column on contact_profile
    sql = _compiled_sql(
        {"all": [{"field": "engagement_tier", "eq": "active"}]},
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert "cp.engagement_tier" in sql
    assert "=" in sql


def test_compile_cp_last_open_at_not_within():
    from datetime import datetime, timezone

    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    sql = _compiled_sql(
        {"all": [{"field": "last_open_at", "op": "not_within", "value": "60d"}]},
        now=now,
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert "cp.last_open_at" in sql
    assert "<" in sql


def test_compile_v2_in_and_not_in():
    sql_in = _compiled_sql(
        {"all": [{"field": "engagement_tier", "op": "in", "value": ["active", "engaged"]}]},
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert "ANY" in sql_in.upper()

    sql_not_in = _compiled_sql(
        {"all": [{"field": "engagement_tier", "op": "not_in", "value": ["cold"]}]},
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert "ALL" in sql_not_in.upper()


def test_compile_cp_mailbox_provider_direct_column():
    sql = _compiled_sql(
        {"all": [{"field": "mailbox_provider", "eq": "gmail"}]},
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert "cp.mailbox_provider" in sql


def test_compile_cp_last_open_at_before():
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    sql = _compiled_sql(
        {"all": [{"field": "last_open_at", "before": "P60D"}]},
        now=now,
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert "cp.last_open_at" in sql
    assert "<" in sql


def test_compile_v2_no_engagement_since_uses_not_exists():
    # no_engagement_since is now normalized to a not_within clause on
    # last_open_at — the compiled SQL must be a timestamp comparison, not
    # a NOT EXISTS subquery against the events table.
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        sql = _compiled_sql(
            {"all": [{"kind": "no_engagement_since", "window": "P60D"}]},
            now=now,
            include_unsendable=True,
            acknowledges_cold=True,
        )
    assert "cp.last_open_at" in sql
    assert "<" in sql
    assert "NOT EXISTS" not in sql.upper()
    assert "events" not in sql.lower()


def test_compile_v2_any_uses_or():
    sql = _compiled_sql(
        {
            "any": [
                {"field": "engagement_tier", "eq": "active"},
                {"field": "engagement_tier", "eq": "engaged"},
            ]
        },
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert " OR " in sql.upper()


def test_compile_v2_all_uses_and():
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    sql = _compiled_sql(
        {
            "all": [
                {"field": "engagement_tier", "eq": "active"},
                {"field": "last_open_at", "op": "not_within", "value": "60d"},
            ]
        },
        now=now,
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert " AND " in sql.upper()


def test_compile_invalid_segment_raises():
    with pytest.raises(ValueError):
        compile_segment({"all": "not-a-list"}, _DEFAULT_CONFIG)


from app.domain.isp import MAILBOX_PROVIDER_CODES as ISP_MP_CODES
from app.domain.isp import REPUTATION_GROUP_CODES as ISP_RG_CODES
from app.services.segment_query import MAILBOX_PROVIDER_CODES, REPUTATION_GROUP_CODES


def test_provider_code_maps_agree_with_isp():
    """The compiler must emit the codes the ingest path actually writes.

    isp.py mirrors the domain_provider seed in
    alembic/versions/2026_07_30_1500-seg1_domain_provider.py.
    """
    for name, code in ISP_MP_CODES.items():
        assert MAILBOX_PROVIDER_CODES[name] == code, name
    for name, code in ISP_RG_CODES.items():
        assert REPUTATION_GROUP_CODES[name] == code, name


def test_cable_compiles_to_the_seeded_cable_code():
    sql, params = compile_segment(
        {"all": [{"field": "mailbox_provider", "eq": "cable"}]},
        _DEFAULT_CONFIG,
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert 4 in params.values()
    assert 5 not in params.values()


def test_attributes_numeric_field_compiles_to_a_cast_subquery():
    sql, params = compile_segment(
        {"all": [{"field": "attributes.vehicle_count", "op": "gte", "value": 2}]},
        _DEFAULT_CONFIG,
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert "FROM recipients r" in sql
    assert "r.id = cp.contact_id" in sql
    assert "r.tenant = cp.tenant" in sql
    assert "::numeric" in sql
    assert "vehicle_count" in params.values()
    assert 2 in params.values()


def test_attributes_contains_compiles_to_jsonb_exists():
    sql, params = compile_segment(
        {"all": [{"field": "attributes.vehicle_makes", "op": "contains", "value": "toyota"}]},
        _DEFAULT_CONFIG,
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert "jsonb_exists(" in sql
    assert "?" not in sql  # the ? operator collides with driver placeholders
    assert "toyota" in params.values()


def test_attributes_key_is_validated():
    with pytest.raises(ValueError, match="invalid attributes key"):
        compile_segment(
            {"all": [{"field": "attributes.bad key'; drop table", "op": "eq", "value": 1}]},
            _DEFAULT_CONFIG,
        )
