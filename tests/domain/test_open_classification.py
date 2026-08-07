"""Pure unit tests for app/domain/open_classification.classify_open().

Covers SPEC-segmentation.md §12 scenario 3 (MPP open detection).

No DB, no FastAPI — pure domain logic only.
"""

from __future__ import annotations

from app.domain.open_classification import CLASS_HUMAN, CLASS_MPP, classify_open

# Apple proxy CIDR block used in all tests that need it.
_APPLE_CIDRS = ["17.0.0.0/8", "17.58.0.0/16"]


def test_scenario_3a_apple_proxy_ip_returns_mpp() -> None:
    """Scenario 3a: IP in a known Apple proxy CIDR → CLASS_MPP (1).

    NOTE: When classify_open returns CLASS_MPP, the caller (SQS poller / webhook
    handler) increments opens_mpp, not opens. The opens counter and last_open_at
    are left unchanged. This function returns the classification only; counter
    routing is the responsibility of the ingest layer.
    """
    result = classify_open(
        ip="17.0.0.1",
        user_agent="Generic-UA/1.0",
        apple_proxy_cidrs=_APPLE_CIDRS,
    )
    assert result == CLASS_MPP


def test_scenario_3b_apple_mail_ua_returns_mpp() -> None:
    """Scenario 3b: UA containing 'Apple' and 'Mail' → CLASS_MPP (1)."""
    result = classify_open(
        ip="8.8.8.8",  # non-Apple IP
        user_agent="Apple Mail/3696.120.41.1.1 (macOS 14.5)",
        apple_proxy_cidrs=_APPLE_CIDRS,
    )
    assert result == CLASS_MPP


def test_scenario_3b_apple_icloud_ua_returns_mpp() -> None:
    """Scenario 3b variant: UA containing 'Apple' and 'iCloud' → CLASS_MPP (1)."""
    result = classify_open(
        ip=None,
        user_agent="Apple iCloud/1.0",
        apple_proxy_cidrs=_APPLE_CIDRS,
    )
    assert result == CLASS_MPP


def test_scenario_3c_non_apple_ip_and_ua_returns_human() -> None:
    """Scenario 3c: Non-Apple IP and non-Apple UA → CLASS_HUMAN (0)."""
    result = classify_open(
        ip="203.0.113.5",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Thunderbird/115.0",
        apple_proxy_cidrs=_APPLE_CIDRS,
    )
    assert result == CLASS_HUMAN


def test_scenario_3d_none_ip_and_ua_returns_human() -> None:
    """Scenario 3d: ip=None, user_agent=None → CLASS_HUMAN (0).

    Missing signals default to human; we don't suppress opens from unknown
    devices. Penalising absence of metadata would exclude legitimate mobile
    clients with privacy controls.
    """
    result = classify_open(
        ip=None,
        user_agent=None,
        apple_proxy_cidrs=_APPLE_CIDRS,
    )
    assert result == CLASS_HUMAN


def test_apple_keyword_without_apple_brand_returns_human() -> None:
    """UA with 'Mail' but not 'Apple' does not trigger MPP detection."""
    result = classify_open(
        ip="1.2.3.4",
        user_agent="Thunderbird Mail/115.0",
        apple_proxy_cidrs=_APPLE_CIDRS,
    )
    assert result == CLASS_HUMAN


def test_unparseable_ip_falls_through_to_ua_check() -> None:
    """An unparseable IP does not raise; UA check still runs."""
    result = classify_open(
        ip="not-an-ip",
        user_agent="Apple Mail/3696",
        apple_proxy_cidrs=_APPLE_CIDRS,
    )
    assert result == CLASS_MPP


def test_empty_cidrs_list_ignores_ip_check() -> None:
    """When apple_proxy_cidrs is empty, no IP is flagged as Apple."""
    result = classify_open(
        ip="17.0.0.1",  # would match if cidrs were provided
        user_agent="Generic-UA/1.0",
        apple_proxy_cidrs=[],
    )
    assert result == CLASS_HUMAN
