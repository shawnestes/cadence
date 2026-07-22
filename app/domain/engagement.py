"""Engagement-decay tiers and pacing (SPEC_ADDENDUM_002 §2).

Pure domain logic: no FastAPI, no SQLAlchemy. The nightly scorer worker
classifies recipients; the send worker maps tier -> minimum gap.
"""

from __future__ import annotations

from datetime import datetime, timedelta

TIER_ENGAGED = "engaged"
TIER_NEUTRAL = "neutral"
TIER_COLD = "cold"
TIER_DORMANT = "dormant"

ALL_TIERS = (TIER_ENGAGED, TIER_NEUTRAL, TIER_COLD, TIER_DORMANT)

_MIN_GAP: dict[str, timedelta] = {
    TIER_ENGAGED: timedelta(hours=24),
    TIER_NEUTRAL: timedelta(hours=72),
    TIER_COLD: timedelta(days=7),
    # dormant recipients are auto-suppressed; gap is moot but defined for safety
    TIER_DORMANT: timedelta(days=365),
}


def min_gap_for_tier(tier: str) -> timedelta:
    """Minimum time between sends for a recipient in `tier`.

    Unknown tiers fall back to the neutral gap (fail-safe: slower, not faster).
    """
    return _MIN_GAP.get(tier, _MIN_GAP[TIER_NEUTRAL])


def classify(
    *,
    last_engagement_at: datetime | None,
    consecutive_unengaged_sends: int,
    now: datetime,
    engaged_window_days: int = 30,
    cold_after_unopened: int = 5,
    dormant_after_unopened: int = 10,
) -> str:
    """Classify a recipient's engagement tier.

    `consecutive_unengaged_sends`: sends since (and not counting) the last
    send that received an open/click, or total sends if none ever engaged.
    """
    if dormant_after_unopened and consecutive_unengaged_sends >= dormant_after_unopened:
        return TIER_DORMANT
    if last_engagement_at is not None and now - last_engagement_at <= timedelta(
        days=engaged_window_days
    ):
        return TIER_ENGAGED
    if cold_after_unopened and consecutive_unengaged_sends >= cold_after_unopened:
        return TIER_COLD
    return TIER_NEUTRAL
