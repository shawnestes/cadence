"""Pure unit tests for app/domain/click_classification.classify_click().

Covers SPEC-segmentation.md §12 scenarios 5 and 6 (click classification).

No DB, no FastAPI — pure domain logic only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.click_classification import CLASS_HUMAN, CLASS_SCANNER, classify_click

# Fixed timestamps used across tests so assertions are deterministic.
_DELIVERED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_CLICKED_AT_SAFE = _DELIVERED_AT + timedelta(seconds=1)   # within 2s — triggers rule 3
_CLICKED_AT_NORMAL = _DELIVERED_AT + timedelta(seconds=5)  # safe delta


def _classify(
    *,
    user_agent: str | None = "Mozilla/5.0 (compatible; benign-client/1.0)",
    ip: str | None = "203.0.113.5",
    clicked_at: datetime = _CLICKED_AT_NORMAL,
    delivered_at: datetime | None = _DELIVERED_AT,
    other_links_same_second: int = 0,
    has_origin_platform_confirm: bool = False,
    confirm_window_s: int = 30,
    known_scanner_uas: list[str] | None = None,
    apple_proxy_cidrs: list[str] | None = None,
) -> int:
    return classify_click(
        user_agent=user_agent,
        ip=ip,
        clicked_at=clicked_at,
        delivered_at=delivered_at,
        other_links_same_second=other_links_same_second,
        has_origin_platform_confirm=has_origin_platform_confirm,
        confirm_window_s=confirm_window_s,
        known_scanner_uas=known_scanner_uas or [],
        apple_proxy_cidrs=apple_proxy_cidrs or [],
    )


def test_scenario_5_benign_click_returns_human() -> None:
    """Scenario 5: single link, no delivery-to-click delta < 2s, benign UA → CLASS_HUMAN (0).

    NOTE on deferred rule 1: 'no origin-platform confirmation within scanner_confirm_window_s'
    is NOT evaluated synchronously by classify_click(). The function accepts
    has_origin_platform_confirm for API stability, but ignores it. Rule 1 is
    applied in a separate deferred pass after the confirmation window elapses.
    A click that arrives with has_origin_platform_confirm=False is therefore
    not immediately classified as scanner by this function.
    """
    result = _classify(
        other_links_same_second=0,
        has_origin_platform_confirm=False,
        clicked_at=_CLICKED_AT_NORMAL,
        delivered_at=_DELIVERED_AT,
    )
    assert result == CLASS_HUMAN


def test_scenario_6_burst_of_links_returns_scanner() -> None:
    """Scenario 6: 8 distinct links clicked in the same second → CLASS_SCANNER (2).

    Rule 2 fires when other_links_same_second >= 1. 8 additional links
    (9 total including this one) clearly exceeds the threshold.
    The contact must NOT reach the 'active' tier as a result — by returning
    CLASS_SCANNER the ingest layer routes this into clicks_bot, not clicks,
    keeping the click-weighted engagement score unaffected.
    """
    result = _classify(other_links_same_second=8)
    assert result == CLASS_SCANNER


def test_scenario_5_with_confirm_still_human_no_other_rules() -> None:
    """Scenario 5 variant: confirmed click with benign profile → CLASS_HUMAN (0).

    Confirm flag doesn't change the synchronous result when no other rules fire.
    """
    result = _classify(
        other_links_same_second=0,
        has_origin_platform_confirm=True,
        clicked_at=_CLICKED_AT_NORMAL,
    )
    assert result == CLASS_HUMAN


def test_rule_3_click_under_2s_after_delivery_returns_scanner() -> None:
    """Rule 3: clicked_at - delivered_at < 2s → CLASS_SCANNER (2).

    Using delivered_at=_DELIVERED_AT and clicked_at=_DELIVERED_AT+1s.
    """
    result = _classify(
        clicked_at=_CLICKED_AT_SAFE,  # 1s after delivery — under the 2s threshold
        delivered_at=_DELIVERED_AT,
        other_links_same_second=0,
    )
    assert result == CLASS_SCANNER


def test_rule_3_click_exactly_2s_after_delivery_is_human() -> None:
    """Rule 3 boundary: delta == 2.0s is not < 2.0, so not scanner."""
    result = _classify(
        clicked_at=_DELIVERED_AT + timedelta(seconds=2),
        delivered_at=_DELIVERED_AT,
        other_links_same_second=0,
    )
    assert result == CLASS_HUMAN


def test_rule_3_no_delivery_timestamp_skips_rule() -> None:
    """When delivered_at is None, rule 3 cannot fire — treated as human."""
    result = _classify(
        clicked_at=_DELIVERED_AT,  # same timestamp — would be 0s delta if known
        delivered_at=None,
        other_links_same_second=0,
    )
    assert result == CLASS_HUMAN


def test_rule_4_known_scanner_ua_returns_scanner() -> None:
    """Rule 4: UA matching a known scanner string → CLASS_SCANNER (2)."""
    result = _classify(
        user_agent="Microsoft SafeLinks/2.0",
        known_scanner_uas=["SafeLinks"],
        other_links_same_second=0,
    )
    assert result == CLASS_SCANNER


def test_rule_4_ua_case_insensitive_match() -> None:
    """Rule 4 matching is case-insensitive."""
    result = _classify(
        user_agent="proofpoint-security-gateway/8.0",
        known_scanner_uas=["Proofpoint"],
        other_links_same_second=0,
    )
    assert result == CLASS_SCANNER


def test_one_additional_link_same_second_triggers_rule_2() -> None:
    """Boundary check: even one other link in the same second is scanner."""
    result = _classify(other_links_same_second=1)
    assert result == CLASS_SCANNER


def test_zero_other_links_does_not_trigger_rule_2() -> None:
    """other_links_same_second=0 should NOT trigger rule 2."""
    result = _classify(other_links_same_second=0)
    assert result == CLASS_HUMAN
