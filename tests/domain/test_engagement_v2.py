"""Tests for app/domain/engagement_v2.py classify_tier() (SPEC-segmentation §4).

All tests are pure — no database required.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import load_engagement_config
from app.domain.engagement_v2 import (
    MAILBOX_PROVIDER_CABLE,
    TIER_ACTIVE,
    TIER_COLD,
    TIER_DORMANT,
    TIER_ENGAGED,
    TIER_NEW,
    TIER_PASSIVE,
    TIER_UNKNOWN,
    classify_tier,
)

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def cfg():
    return load_engagement_config()


def _classify(cfg, **kwargs):
    defaults = dict(
        clicks_long=0,
        converts_long=0,
        opens_short=0,
        opens_long=0,
        sends_long=0,
        last_open_at=None,
        sends_lifetime=10,
        first_send_at=NOW - timedelta(days=60),
        mailbox_provider=None,
        now=NOW,
    )
    defaults.update(kwargs)
    return classify_tier(config=cfg, **defaults)


# ---------------------------------------------------------------------------
# 1–8: All 7 tiers (plus active via converts)
# ---------------------------------------------------------------------------


def test_tier_active_via_clicks(cfg):
    """clicks_long >= 1 → TIER_ACTIVE."""
    assert _classify(cfg, clicks_long=1) == TIER_ACTIVE


def test_tier_active_via_converts(cfg):
    """converts_long >= 1, zero clicks → still TIER_ACTIVE."""
    assert _classify(cfg, converts_long=1, clicks_long=0) == TIER_ACTIVE


def test_tier_engaged(cfg):
    """opens_short >= 1, no clicks/converts → TIER_ENGAGED."""
    assert _classify(cfg, opens_short=1, clicks_long=0, converts_long=0) == TIER_ENGAGED


def test_tier_passive(cfg):
    """opens_long >= 1 but opens_short=0 → TIER_PASSIVE."""
    assert _classify(cfg, opens_long=1, opens_short=0) == TIER_PASSIVE


def test_tier_dormant(cfg):
    """sends_long >= 3, zero opens, no last_open_at → TIER_DORMANT."""
    assert (
        _classify(
            cfg,
            sends_long=3,
            opens_long=0,
            opens_short=0,
            last_open_at=None,
            sends_lifetime=10,
            first_send_at=NOW - timedelta(days=60),
        )
        == TIER_DORMANT
    )


def test_tier_cold(cfg):
    """sends_lifetime >= 5, never opened, tenure >= 30d → TIER_COLD."""
    assert (
        _classify(
            cfg,
            sends_long=0,
            opens_long=0,
            opens_short=0,
            last_open_at=None,
            sends_lifetime=5,
            first_send_at=NOW - timedelta(days=31),
        )
        == TIER_COLD
    )


def test_tier_new(cfg):
    """sends_lifetime < new_max_sends_lifetime (3) → TIER_NEW."""
    assert (
        _classify(
            cfg,
            sends_long=0,
            opens_long=0,
            opens_short=0,
            last_open_at=None,
            sends_lifetime=2,
            first_send_at=None,
        )
        == TIER_NEW
    )


def test_tier_unknown_fallthrough(cfg):
    """sends_lifetime=10, no activity, tenure only 5d → falls through to TIER_UNKNOWN.

    Tenure guard blocks COLD (5d < 30d). sends_long=0 blocks DORMANT.
    sends_lifetime=10 >= 3 blocks NEW. Falls to UNKNOWN.
    """
    assert (
        _classify(
            cfg,
            sends_long=0,
            opens_long=0,
            opens_short=0,
            last_open_at=None,
            sends_lifetime=10,
            first_send_at=NOW - timedelta(days=5),
        )
        == TIER_UNKNOWN
    )


# ---------------------------------------------------------------------------
# Tenure guard scenarios (spec §12 tests 30, 31, 32)
# ---------------------------------------------------------------------------


def test_tenure_guard_30_not_cold_young_contact(cfg):
    """Spec §12 test 30: sends_lifetime=6, tenure=12d, opens_long=0.

    Tenure guard blocks COLD (12 < 30). sends_long=6 >= 3 → TIER_DORMANT.
    """
    result = _classify(
        cfg,
        sends_long=6,
        opens_long=0,
        opens_short=0,
        last_open_at=None,
        sends_lifetime=6,
        first_send_at=NOW - timedelta(days=12),
    )
    assert result == TIER_DORMANT


def test_tenure_guard_31_cold_old_contact(cfg):
    """Spec §12 test 31: sends_lifetime=6, tenure=60d, opens_long=0 → TIER_COLD.

    DORMANT fires first if sends_long >= 3. To reach COLD, sends_long must be
    below dormant_min_sends_long (3). Use sends_long=2 so the ladder falls
    through DORMANT and reaches COLD (lifetime=6 >= 5, tenure=60 >= 30).
    """
    result = _classify(
        cfg,
        sends_long=2,
        opens_long=0,
        opens_short=0,
        last_open_at=None,
        sends_lifetime=6,
        first_send_at=NOW - timedelta(days=60),
    )
    assert result == TIER_COLD


def test_tenure_guard_32_low_send_count(cfg):
    """Spec §12 test 32: sends_lifetime=3, tenure=200d, opens_long=0.

    cold_min_sends_lifetime=5, so 3 < 5 blocks COLD.
    sends_long=3 >= dormant_min_sends_long=3 → TIER_DORMANT.
    """
    result = _classify(
        cfg,
        sends_long=3,
        opens_long=0,
        opens_short=0,
        last_open_at=None,
        sends_lifetime=3,
        first_send_at=NOW - timedelta(days=200),
    )
    assert result == TIER_DORMANT


# ---------------------------------------------------------------------------
# Cable override tests (spec §12 test 15e, D10)
# ---------------------------------------------------------------------------


def test_cable_override_dormant_not_cold(cfg):
    """Cable provider: 6 lifetime sends < cable threshold (8) → NOT cold → TIER_UNKNOWN.

    cable override sets cold_min_sends_lifetime=8. With sends_lifetime=6 < 8,
    COLD is skipped. sends_long=2 < 3 so DORMANT is also skipped.
    sends_lifetime=6 >= new_max (3) so NEW is skipped → TIER_UNKNOWN.

    Contrast with the gmail test below where the same parameters → TIER_COLD
    because the base threshold (5) is met.
    """
    result = _classify(
        cfg,
        sends_long=2,
        opens_long=0,
        opens_short=0,
        last_open_at=None,
        sends_lifetime=6,
        first_send_at=NOW - timedelta(days=60),
        mailbox_provider=MAILBOX_PROVIDER_CABLE,
    )
    assert result == TIER_UNKNOWN


def test_gmail_cold_at_6_sends(cfg):
    """Gmail (provider=1): 6 lifetime sends >= base threshold (5), tenure=60d → TIER_COLD.

    sends_long=2 (below dormant_min=3) so DORMANT is skipped; COLD fires
    because lifetime=6 >= 5 and tenure=60 >= 30 and last_open_at=None.
    """
    result = _classify(
        cfg,
        sends_long=2,
        opens_long=0,
        opens_short=0,
        last_open_at=None,
        sends_lifetime=6,
        first_send_at=NOW - timedelta(days=60),
        mailbox_provider=1,  # gmail, not cable
    )
    assert result == TIER_COLD


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


def test_active_beats_dormant_conditions(cfg):
    """If both active and dormant conditions are met, TIER_ACTIVE wins (first-match)."""
    result = _classify(
        cfg,
        clicks_long=1,
        sends_long=5,
        opens_long=0,
    )
    assert result == TIER_ACTIVE


def test_cold_requires_last_open_at_none(cfg):
    """Cold requires last_open_at=None. If they had a prior open, COLD is blocked."""
    result = _classify(
        cfg,
        sends_long=0,
        opens_long=0,
        opens_short=0,
        last_open_at=NOW - timedelta(days=365),
        sends_lifetime=10,
        first_send_at=NOW - timedelta(days=60),
    )
    # last_open_at is set → cold blocked; sends_long=0 → dormant blocked; lifetime>=3 → UNKNOWN
    assert result == TIER_UNKNOWN


def test_cold_requires_first_send_at(cfg):
    """Cold requires first_send_at not None for tenure calculation."""
    result = _classify(
        cfg,
        sends_long=0,
        opens_long=0,
        opens_short=0,
        last_open_at=None,
        sends_lifetime=10,
        first_send_at=None,
    )
    # first_send_at=None → cold blocked; falls to UNKNOWN
    assert result == TIER_UNKNOWN
