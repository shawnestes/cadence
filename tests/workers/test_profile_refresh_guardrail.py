"""Tests for the blast-radius guardrail in app/workers/profile_refresh.py (D4).

Tests _check_guardrail() directly — no database required.
"""

from __future__ import annotations

import pytest

from app.workers.profile_refresh import _check_guardrail

MAX_PCT = 0.10  # 10% threshold matching engagement.yaml default


# ---------------------------------------------------------------------------
# Basic breach / no-breach scenarios
# ---------------------------------------------------------------------------


def test_no_shift_no_breach():
    """Identical counts → no breach."""
    old = {1: 1000, 2: 500, 3: 200}
    new = {1: 1000, 2: 500, 3: 200}
    assert _check_guardrail(old, new, MAX_PCT) is False


def test_nine_percent_shift_no_breach():
    """9% shift is below 10% threshold → no breach."""
    old = {1: 1000}
    new = {1: 910}  # 9% decrease
    assert _check_guardrail(old, new, MAX_PCT) is False


def test_eleven_percent_shift_breach():
    """11% shift exceeds 10% threshold → breach."""
    old = {1: 1000}
    new = {1: 890}  # 11% decrease
    assert _check_guardrail(old, new, MAX_PCT) is True


def test_exactly_ten_percent_no_breach():
    """Shift of exactly 10% is NOT a breach (> not >=)."""
    old = {1: 1000}
    new = {1: 900}  # exactly 10% decrease
    assert _check_guardrail(old, new, MAX_PCT) is False


# ---------------------------------------------------------------------------
# Zero-old-count edge cases
# ---------------------------------------------------------------------------


def test_old_count_zero_skipped():
    """Tier with old_count=0 is skipped to avoid division-by-zero."""
    old = {1: 0}
    new = {1: 100}
    # Would be division-by-zero without the guard; should return False (no breach)
    assert _check_guardrail(old, new, MAX_PCT) is False


def test_new_tier_that_had_zero_old_count_no_breach():
    """Tier that appears in new but had 0 old count → skipped, no breach."""
    old = {1: 1000}
    new = {1: 1000, 2: 500}  # tier 2 is brand new
    # tier 2: old=0 → skipped; tier 1: no shift → no breach
    assert _check_guardrail(old, new, MAX_PCT) is False


def test_empty_old_counts_no_breach():
    """If old_counts is entirely empty, no division possible → no breach."""
    old: dict[int, int] = {}
    new = {1: 500, 2: 300}
    assert _check_guardrail(old, new, MAX_PCT) is False


# ---------------------------------------------------------------------------
# Multi-tier scenarios
# ---------------------------------------------------------------------------


def test_one_tier_breaches_out_of_many():
    """Even one tier breaching returns True."""
    old = {1: 1000, 2: 500, 3: 200}
    new = {1: 1000, 2: 500, 3: 160}  # tier 3: 20% drop → breach
    assert _check_guardrail(old, new, MAX_PCT) is True


def test_all_tiers_within_threshold():
    """All tiers shift ~8% → no breach."""
    old = {1: 1000, 2: 500, 3: 200}
    new = {1: 920, 2: 460, 3: 184}
    assert _check_guardrail(old, new, MAX_PCT) is False


def test_increase_also_checked():
    """An increase of 11% also triggers breach."""
    old = {1: 1000}
    new = {1: 1110}  # 11% increase
    assert _check_guardrail(old, new, MAX_PCT) is True
