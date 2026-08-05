"""Tests for app/domain/engagement_score.py (decay + compute_score).

All tests are pure — no database required.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import load_engagement_config
from app.domain.engagement_score import compute_score, decay

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def score_cfg():
    return load_engagement_config().score


# ---------------------------------------------------------------------------
# decay()
# ---------------------------------------------------------------------------


def test_decay_none_event_is_zero():
    """decay(None, ...) always returns 0.0."""
    assert decay(None, 30, NOW) == 0.0


def test_decay_at_event_time_is_one():
    """decay(now, halflife, now) == 1.0 (zero elapsed days)."""
    assert decay(NOW, 30, NOW) == 1.0


def test_decay_one_halflife_is_half():
    """decay at exactly one halflife elapsed ≈ 0.5."""
    event_at = NOW - timedelta(days=30)
    result = decay(event_at, 30, NOW)
    assert abs(result - 0.5) < 1e-9


def test_decay_two_halflives_is_quarter():
    """decay at two halflives ≈ 0.25."""
    event_at = NOW - timedelta(days=60)
    result = decay(event_at, 30, NOW)
    assert abs(result - 0.25) < 1e-9


# ---------------------------------------------------------------------------
# compute_score()
# ---------------------------------------------------------------------------


def test_compute_score_all_nulls(score_cfg):
    """All inputs null → score is 0."""
    result = compute_score(
        last_click_at=None,
        last_open_at=None,
        last_convert_at=None,
        complaints_90d=0,
        bounces_90d=0,
        config=score_cfg,
        now=NOW,
    )
    assert result == 0


def test_compute_score_complaint_penalty(score_cfg):
    """complaints_90d=1 → penalty_complaint (0.0) multiplier → score is 0."""
    result = compute_score(
        last_click_at=NOW,
        last_open_at=NOW,
        last_convert_at=NOW,
        complaints_90d=1,
        bounces_90d=0,
        config=score_cfg,
        now=NOW,
    )
    # penalty_complaint=0.0 → raw * 0.0 = 0
    assert result == 0


def test_compute_score_bounce_penalty(score_cfg):
    """bounces_90d=1, no complaints → penalty_bounce (0.5) applied."""
    result = compute_score(
        last_click_at=NOW,
        last_open_at=None,
        last_convert_at=None,
        complaints_90d=0,
        bounces_90d=1,
        config=score_cfg,
        now=NOW,
    )
    # raw = 100 * (0.5 * 1.0 + 0 + 0) * 0.5 = 25
    expected = round(100.0 * score_cfg.weight_click * 1.0 * score_cfg.penalty_bounce)
    assert result == expected


def test_compute_score_clamped_to_100(score_cfg):
    """Score can't exceed 100 even with all signals fresh."""
    result = compute_score(
        last_click_at=NOW,
        last_open_at=NOW,
        last_convert_at=NOW,
        complaints_90d=0,
        bounces_90d=0,
        config=score_cfg,
        now=NOW,
    )
    assert result <= 100


def test_compute_score_clamped_to_zero(score_cfg):
    """Score is never negative."""
    result = compute_score(
        last_click_at=None,
        last_open_at=None,
        last_convert_at=None,
        complaints_90d=0,
        bounces_90d=0,
        config=score_cfg,
        now=NOW,
    )
    assert result >= 0


def test_compute_score_fresh_click_no_penalty(score_cfg):
    """Fresh click with no penalty → score equals weight_click * 100."""
    result = compute_score(
        last_click_at=NOW,
        last_open_at=None,
        last_convert_at=None,
        complaints_90d=0,
        bounces_90d=0,
        config=score_cfg,
        now=NOW,
    )
    expected = round(100.0 * score_cfg.weight_click * 1.0)
    assert result == expected


def test_compute_score_complaint_beats_bounce(score_cfg):
    """When both complaints and bounces present, complaint penalty (0.0) applies."""
    result = compute_score(
        last_click_at=NOW,
        last_open_at=NOW,
        last_convert_at=NOW,
        complaints_90d=1,
        bounces_90d=1,
        config=score_cfg,
        now=NOW,
    )
    assert result == 0
