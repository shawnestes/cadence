"""Pure tests for MAB selection and retirement logic."""

from __future__ import annotations

import random

from app.domain.mab import VariantStats, decide_retirement, select_variant


def _vs(label, sent=0, clicks=0, opens=0, bounces=0, complaints=0):
    return VariantStats(
        variant_label=label,
        sent=sent,
        unique_clicks=clicks,
        unique_opens=opens,
        bounces=bounces,
        complaints=complaints,
    )


def test_select_variant_forces_explore_when_underexplored():
    stats = [_vs("A", sent=10), _vs("B", sent=50), _vs("C", sent=100)]
    chosen = select_variant(stats, min_explore_sends=200)
    assert chosen in {"A", "B", "C"}


def test_select_variant_empty_returns_none():
    assert select_variant([]) is None


def test_select_variant_picks_highest_expected_click_rate_over_many_trials():
    random.seed(42)
    stats = [
        _vs("A", sent=1000, clicks=500),
        _vs("B", sent=1000, clicks=5),
    ]
    wins_a = 0
    trials = 200
    for _ in range(trials):
        if select_variant(stats, min_explore_sends=200) == "A":
            wins_a += 1
    assert wins_a / trials > 0.8


def test_decide_retirement_flags_bounce_rate():
    stats = [_vs("A", sent=1000, clicks=50, bounces=30)]
    decisions = decide_retirement(stats, min_retire_sends=1000)
    assert len(decisions) == 1
    assert decisions[0].retire is True
    assert decisions[0].reason == "bounce_rate"


def test_decide_retirement_flags_complaint_rate():
    stats = [_vs("A", sent=1000, clicks=50, bounces=0, complaints=5)]
    decisions = decide_retirement(stats, min_retire_sends=1000)
    assert decisions[0].retire is True
    assert decisions[0].reason == "complaint_rate"


def test_decide_retirement_skips_under_min_sends():
    stats = [_vs("A", sent=500, clicks=5, bounces=50)]
    decisions = decide_retirement(stats, min_retire_sends=1000)
    assert decisions[0].retire is False
    assert decisions[0].reason is None


def test_decide_retirement_click_rate_vs_best():
    stats = [
        _vs("A", sent=2000, clicks=200),
        _vs("B", sent=2000, clicks=1000),
    ]
    decisions = decide_retirement(stats, min_retire_sends=1000)
    by_label = {d.variant_label: d for d in decisions}
    assert by_label["A"].retire is True
    assert by_label["A"].reason == "click_rate_vs_best"
    assert by_label["B"].retire is False
