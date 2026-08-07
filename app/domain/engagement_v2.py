"""7-tier engagement classification (SPEC-segmentation.md §4).

Pure domain logic: no FastAPI, no SQLAlchemy.
Ladder is first-match-wins. Cable provider override (D10) adjusts thresholds
before evaluation when mailbox_provider == MAILBOX_PROVIDER_CABLE.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import EngagementConfig

# Tier smallint codes — first-match-wins ladder in classify_tier().
TIER_ACTIVE = 1
TIER_ENGAGED = 2
TIER_PASSIVE = 3
TIER_DORMANT = 4
TIER_COLD = 5
TIER_NEW = 6
TIER_UNKNOWN = 7

# Cable mailbox_provider smallint code matches domain_provider seed data (§3).
MAILBOX_PROVIDER_CABLE = 5


def classify_tier(
    *,
    clicks_long: int,
    converts_long: int,
    opens_short: int,
    opens_long: int,
    sends_long: int,
    last_open_at: datetime | None,
    sends_lifetime: int,
    first_send_at: datetime | None,
    mailbox_provider: int | None,
    config: EngagementConfig,
    now: datetime | None = None,
) -> int:
    """Classify a contact into one of 7 engagement tiers.

    Returns the tier smallint code. Ladder is first-match-wins.
    Cable override (D10): when mailbox_provider matches MAILBOX_PROVIDER_CABLE,
    threshold overrides from config.overrides.by_mailbox_provider.cable are
    merged into the base thresholds before evaluating the ladder.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Build effective thresholds, starting from base config.
    thresholds = config.thresholds.model_dump()

    # Apply cable override when applicable (D10).
    if mailbox_provider == MAILBOX_PROVIDER_CABLE:
        cable_overrides: dict = (
            config.overrides.by_mailbox_provider.model_extra.get("cable") or {}
        )
        thresholds.update(cable_overrides)

    # 1. active: any click or conversion in the long window.
    if (
        clicks_long >= thresholds["active_min_clicks_long"]
        or converts_long >= thresholds["active_min_converts_long"]
    ):
        return TIER_ACTIVE

    # 2. engaged: at least one open in the short window.
    if opens_short >= thresholds["engaged_min_opens_short"]:
        return TIER_ENGAGED

    # 3. passive: at least one open in the long window.
    if opens_long >= thresholds["passive_min_opens_long"]:
        return TIER_PASSIVE

    # 4. dormant: enough sends in the long window but zero opens.
    if sends_long >= thresholds["dormant_min_sends_long"] and opens_long == 0:
        return TIER_DORMANT

    # 5. cold: never opened, enough lifetime sends, and old enough (terminal).
    if (
        last_open_at is None
        and sends_lifetime >= thresholds["cold_min_sends_lifetime"]
        and first_send_at is not None
        and (now - first_send_at).days >= thresholds["cold_min_tenure_days"]
    ):
        return TIER_COLD

    # 6. new: very few lifetime sends.
    if sends_lifetime < thresholds["new_max_sends_lifetime"]:
        return TIER_NEW

    # 7. unknown: fallthrough.
    return TIER_UNKNOWN
