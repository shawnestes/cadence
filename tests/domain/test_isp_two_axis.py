"""Unit tests for classify_domain_two_axis covering SPEC §12 scenarios 10-12.

No DB, no fixtures beyond parametrize.  All imports are pure-domain only.
"""

from __future__ import annotations

import pytest

from app.domain.isp import classify_domain_two_axis


# ---------------------------------------------------------------------------
# Scenario 10: sbcglobal.net and AT&T-family → ('cable', 'yahoo')
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "domain",
    [
        "sbcglobal.net",
        "att.net",
        "bellsouth.net",
        "verizon.net",
    ],
)
def test_scenario_10_att_family_cable_yahoo(domain: str) -> None:
    """AT&T-family + verizon.net are cable brand, yahoo reputation group (§3)."""
    assert classify_domain_two_axis(domain) == ("cable", "yahoo")


# ---------------------------------------------------------------------------
# Scenario 10 companions: generic cable ISPs → ('cable', 'other')
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "domain",
    [
        "comcast.net",
        "charter.net",
        "cox.net",
    ],
)
def test_scenario_10_generic_cable_other(domain: str) -> None:
    """Generic cable operators have cable brand but reputation_group=other until MX refines."""
    assert classify_domain_two_axis(domain) == ("cable", "other")


# ---------------------------------------------------------------------------
# Scenario 11: corporate domain (Workspace-hosted) — stub / TODO
# ---------------------------------------------------------------------------

def test_scenario_11_corporate_domain_stub() -> None:
    """Scenario 11: acme.com hosted on Google Workspace → ('corporate', 'gmail').

    TODO(T4): classify_domain_two_axis is seed-dict-based only; unknown domains
    fall back to ('other', 'other').  When T4 adds async MX lookup and writes
    the result to domain_provider, a row ('acme.com', 'corporate', 'gmail',
    'mx_lookup') will be seeded into the dict at startup or injected here as a
    pre-populated domain_provider fixture.  Until then we assert the *fallback*
    behaviour — the function must not raise, and must not return a corporate
    classification it hasn't earned via MX resolution.
    """
    result = classify_domain_two_axis("acme.com")
    # acme.com is not in the seed table; dict-only implementation returns other/other.
    assert result == ("other", "other")


# ---------------------------------------------------------------------------
# Scenario 12: unknown domain → ('other', 'other')
# ---------------------------------------------------------------------------

def test_scenario_12_unknown_domain() -> None:
    """Completely unknown domain falls back to ('other', 'other') with zero latency."""
    assert classify_domain_two_axis("randomdomain.example") == ("other", "other")


# ---------------------------------------------------------------------------
# Normalization: whitespace, uppercase, trailing dot
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw_domain",
    [
        "sbcglobal.net",        # canonical
        " sbcglobal.net ",      # leading/trailing whitespace
        "SBCGLOBAL.NET",        # uppercase
        "sbcglobal.net.",       # trailing dot (FQDN style)
        "  SBCGLOBAL.NET. ",    # combined
    ],
)
def test_normalization_whitespace_case_trailing_dot(raw_domain: str) -> None:
    """All normalized forms of sbcglobal.net yield the same two-axis result."""
    assert classify_domain_two_axis(raw_domain) == ("cable", "yahoo")


# ---------------------------------------------------------------------------
# Yahoo ccTLD coverage — explicit seed entries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "domain",
    [
        "yahoo.co.uk",
        "yahoo.co.jp",
        "yahoo.de",
    ],
)
def test_yahoo_cctld_coverage(domain: str) -> None:
    """Seeded Yahoo ccTLDs return ('yahoo', 'yahoo')."""
    assert classify_domain_two_axis(domain) == ("yahoo", "yahoo")


# ---------------------------------------------------------------------------
# Non-seed yahoo variant: no startswith fallback (spec note in task)
# ---------------------------------------------------------------------------

def test_yahoo_xyz_no_startswith_fallback() -> None:
    """yahoo.xyz is NOT in the seed table; no prefix matching → ('other', 'other').

    The spec explicitly forbids a yahoo.* startswith fallback to avoid
    false positives from arbitrary yahoo-prefixed domains.
    """
    assert classify_domain_two_axis("yahoo.xyz") == ("other", "other")


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------

def test_empty_string_returns_other() -> None:
    """Empty domain string does not crash and returns ('other', 'other')."""
    assert classify_domain_two_axis("") == ("other", "other")


def test_gmail_com_basic() -> None:
    """Canonical gmail.com → ('gmail', 'gmail')."""
    assert classify_domain_two_axis("gmail.com") == ("gmail", "gmail")


def test_microsoft_outlook_com() -> None:
    """outlook.com → ('microsoft', 'microsoft')."""
    assert classify_domain_two_axis("outlook.com") == ("microsoft", "microsoft")


def test_yahoo_com_basic() -> None:
    """yahoo.com → ('yahoo', 'yahoo')."""
    assert classify_domain_two_axis("yahoo.com") == ("yahoo", "yahoo")


def test_apple_icloud() -> None:
    """icloud.com → ('apple', 'apple')."""
    assert classify_domain_two_axis("icloud.com") == ("apple", "apple")
