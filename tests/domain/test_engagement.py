"""Pure tests for app/domain/engagement.py (classify() + min_gap_for_tier())."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.engagement import (
    TIER_COLD,
    TIER_DORMANT,
    TIER_ENGAGED,
    TIER_NEUTRAL,
    classify,
    min_gap_for_tier,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def test_recent_click_is_engaged():
    assert (
        classify(
            last_engagement_at=NOW - timedelta(days=1),
            consecutive_unengaged_sends=0,
            now=NOW,
        )
        == TIER_ENGAGED
    )


def test_five_straight_unengaged_is_cold():
    assert (
        classify(
            last_engagement_at=None,
            consecutive_unengaged_sends=5,
            now=NOW,
        )
        == TIER_COLD
    )


def test_ten_straight_unengaged_is_dormant():
    assert (
        classify(
            last_engagement_at=None,
            consecutive_unengaged_sends=10,
            now=NOW,
        )
        == TIER_DORMANT
    )


def test_dormant_wins_over_recent_engagement():
    """If dormant threshold is met, dormant takes precedence over a recent
    open/click — per classify()'s actual order of checks, the dormant
    unengaged-count check runs before the engaged-window check."""
    assert (
        classify(
            last_engagement_at=NOW - timedelta(days=1),
            consecutive_unengaged_sends=10,
            now=NOW,
        )
        == TIER_DORMANT
    )


def test_no_engagement_ever_and_below_cold_threshold_is_neutral():
    assert (
        classify(
            last_engagement_at=None,
            consecutive_unengaged_sends=4,
            now=NOW,
        )
        == TIER_NEUTRAL
    )


def test_engagement_outside_window_and_below_cold_threshold_is_neutral():
    assert (
        classify(
            last_engagement_at=NOW - timedelta(days=31),
            consecutive_unengaged_sends=4,
            now=NOW,
        )
        == TIER_NEUTRAL
    )


def test_boundary_engaged_window_exactly_30_days_is_still_engaged():
    assert (
        classify(
            last_engagement_at=NOW - timedelta(days=30),
            consecutive_unengaged_sends=0,
            now=NOW,
        )
        == TIER_ENGAGED
    )


def test_boundary_31_days_falls_out_of_engaged_window():
    assert (
        classify(
            last_engagement_at=NOW - timedelta(days=30, seconds=1),
            consecutive_unengaged_sends=0,
            now=NOW,
        )
        != TIER_ENGAGED
    )


def test_boundary_four_unengaged_is_neutral_not_cold():
    assert (
        classify(
            last_engagement_at=None,
            consecutive_unengaged_sends=4,
            now=NOW,
        )
        == TIER_NEUTRAL
    )


def test_boundary_nine_unengaged_is_cold_not_dormant():
    assert (
        classify(
            last_engagement_at=None,
            consecutive_unengaged_sends=9,
            now=NOW,
        )
        == TIER_COLD
    )


def test_custom_thresholds_are_respected():
    assert (
        classify(
            last_engagement_at=None,
            consecutive_unengaged_sends=3,
            now=NOW,
            engaged_window_days=7,
            cold_after_unopened=3,
            dormant_after_unopened=6,
        )
        == TIER_COLD
    )


@pytest.mark.parametrize(
    ("tier", "expected_hours"),
    [
        (TIER_ENGAGED, 24),
        (TIER_NEUTRAL, 72),
        (TIER_COLD, 168),
    ],
)
def test_min_gap_for_known_tiers(tier: str, expected_hours: int):
    assert min_gap_for_tier(tier) == timedelta(hours=expected_hours)


def test_min_gap_for_unknown_tier_falls_back_to_neutral():
    assert min_gap_for_tier("not-a-real-tier") == min_gap_for_tier(TIER_NEUTRAL)
