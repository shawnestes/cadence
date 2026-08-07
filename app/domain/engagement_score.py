from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import EngagementScore


def decay(event_at: datetime | None, halflife_days: float, now: datetime) -> float:
    """Exponential decay: 1.0 at event_at, halves every halflife_days."""
    if event_at is None:
        return 0.0
    elapsed_days = (now - event_at).days
    return 2.0 ** (-elapsed_days / halflife_days)


def compute_score(
    *,
    last_click_at: datetime | None,
    last_open_at: datetime | None,
    last_convert_at: datetime | None,
    complaints_90d: int,
    bounces_90d: int,
    config: EngagementScore,
    now: datetime | None = None,
) -> int:
    """Return engagement score clamped to [0, 100] as an int.

    now defaults to datetime.now(timezone.utc) if not provided.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if complaints_90d > 0:
        penalty = config.penalty_complaint
    elif bounces_90d > 0:
        penalty = config.penalty_bounce
    else:
        penalty = 1.0

    raw = 100.0 * (
        config.weight_click * decay(last_click_at, config.halflife_click_days, now)
        + config.weight_open * decay(last_open_at, config.halflife_open_days, now)
        + config.weight_convert * decay(last_convert_at, config.halflife_convert_days, now)
    ) * penalty

    return max(0, min(100, round(raw)))
