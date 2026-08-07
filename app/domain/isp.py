"""Pure domain→ISP classification for reputation reporting.

No FastAPI/SQLAlchemy imports — this module takes and returns primitives so
it can be unit tested without a DB (see CLAUDE.md "Domain logic is pure").
"""

from __future__ import annotations

__all__ = [
    "classify_isp_from_domain",
    "classify_isp_from_email",
    "classify_isp_from_cloudwatch_label",
    "classify_domain_two_axis",
    "MAILBOX_PROVIDER_CODES",
    "REPUTATION_GROUP_CODES",
    "DOMAIN_SOURCE_SEED",
    "DOMAIN_SOURCE_MX_LOOKUP",
    "DOMAIN_SOURCE_MANUAL",
]

# Integer codes for domain_provider.mailbox_provider (smallint).
# 0 = other/unmatched fallthrough.
MAILBOX_PROVIDER_CODES: dict[str, int] = {
    "gmail": 1,
    "microsoft": 2,
    "yahoo": 3,
    "cable": 4,
    "apple": 5,
    "corporate": 6,
    "other": 0,
}

# Integer codes for domain_provider.reputation_group (smallint).
# Cable ISPs are per-operator: AT&T-family and verizon.net route to yahoo (3);
# other cable operators use other (0) until MX lookup refines them.
REPUTATION_GROUP_CODES: dict[str, int] = {
    "gmail": 1,
    "microsoft": 2,
    "yahoo": 3,
    "apple": 5,
    "other": 0,
}

# domain_provider.source values.
DOMAIN_SOURCE_SEED: int = 1
DOMAIN_SOURCE_MX_LOOKUP: int = 2
DOMAIN_SOURCE_MANUAL: int = 3

# ---------------------------------------------------------------------------
# Two-axis classification table (mailbox_provider, reputation_group).
# Mirrors the seed rows from the seg1_domain_provider migration exactly.
# Keys are normalized (lowercase, no trailing dot). Falls back to
# ('other', 'other') for any domain not present here.
# ---------------------------------------------------------------------------
_DOMAIN_TWO_AXIS: dict[str, tuple[str, str]] = {
    # --- Gmail ---
    "gmail.com": ("gmail", "gmail"),
    "googlemail.com": ("gmail", "gmail"),
    # --- Microsoft ---
    "hotmail.com": ("microsoft", "microsoft"),
    "hotmail.co.uk": ("microsoft", "microsoft"),
    "hotmail.fr": ("microsoft", "microsoft"),
    "hotmail.de": ("microsoft", "microsoft"),
    "msn.com": ("microsoft", "microsoft"),
    "msn.co.uk": ("microsoft", "microsoft"),
    "msn.fr": ("microsoft", "microsoft"),
    "msn.de": ("microsoft", "microsoft"),
    "outlook.com": ("microsoft", "microsoft"),
    "outlook.co.uk": ("microsoft", "microsoft"),
    "outlook.fr": ("microsoft", "microsoft"),
    "outlook.de": ("microsoft", "microsoft"),
    "live.com": ("microsoft", "microsoft"),
    "live.co.uk": ("microsoft", "microsoft"),
    "live.fr": ("microsoft", "microsoft"),
    "live.de": ("microsoft", "microsoft"),
    # --- Yahoo brand + ccTLDs ---
    "yahoo.com": ("yahoo", "yahoo"),
    "yahoo.co.uk": ("yahoo", "yahoo"),
    "yahoo.co.jp": ("yahoo", "yahoo"),
    "yahoo.fr": ("yahoo", "yahoo"),
    "yahoo.de": ("yahoo", "yahoo"),
    "yahoo.it": ("yahoo", "yahoo"),
    "yahoo.es": ("yahoo", "yahoo"),
    "yahoo.com.br": ("yahoo", "yahoo"),
    "yahoo.com.au": ("yahoo", "yahoo"),
    "yahoo.com.mx": ("yahoo", "yahoo"),
    "yahoo.ca": ("yahoo", "yahoo"),
    "ymail.com": ("yahoo", "yahoo"),
    "rocketmail.com": ("yahoo", "yahoo"),
    "aol.com": ("yahoo", "yahoo"),
    "aim.com": ("yahoo", "yahoo"),
    # --- Cable: generic operators (reputation_group = other until MX refines) ---
    "comcast.net": ("cable", "other"),
    "charter.net": ("cable", "other"),
    "spectrum.net": ("cable", "other"),
    "rr.com": ("cable", "other"),
    "twc.com": ("cable", "other"),
    "cox.net": ("cable", "other"),
    "optonline.net": ("cable", "other"),
    "mediacombb.net": ("cable", "other"),
    "windstream.net": ("cable", "other"),
    "frontier.com": ("cable", "other"),
    "earthlink.net": ("cable", "other"),
    "juno.com": ("cable", "other"),
    "netzero.net": ("cable", "other"),
    # --- Cable: AT&T-family + verizon.net → reputation_group = yahoo (§3) ---
    "att.net": ("cable", "yahoo"),
    "sbcglobal.net": ("cable", "yahoo"),
    "bellsouth.net": ("cable", "yahoo"),
    "ameritech.net": ("cable", "yahoo"),
    "pacbell.net": ("cable", "yahoo"),
    "swbell.net": ("cable", "yahoo"),
    "verizon.net": ("cable", "yahoo"),
    # --- Apple ---
    "icloud.com": ("apple", "apple"),
    "me.com": ("apple", "apple"),
    "mac.com": ("apple", "apple"),
}

_YAHOO_MARKERS = ("yahoo.", "ymail.")
_OUTLOOK_MARKERS = ("hotmail.", "outlook.", "live.", "msn.")
_APPLE_DOMAINS = frozenset({"icloud.com", "me.com", "mac.com"})


def classify_isp_from_domain(domain: str) -> str:
    """Map an email domain to a coarse ISP bucket for reputation reporting.

    Args:
        domain: The part of an email address after ``@``, e.g. ``"gmail.com"``.
            Comparison is case-insensitive.

    Returns:
        One of ``"Gmail"``, ``"Yahoo"``, ``"Outlook"``, ``"Apple"``, ``"Other"``.
    """
    normalized = (domain or "").strip().lower()

    if normalized == "gmail.com":
        return "Gmail"
    if any(normalized.startswith(marker) for marker in _YAHOO_MARKERS):
        return "Yahoo"
    if any(normalized.startswith(marker) for marker in _OUTLOOK_MARKERS):
        return "Outlook"
    if normalized in _APPLE_DOMAINS:
        return "Apple"
    return "Other"


def classify_isp_from_email(email: str) -> str:
    """Map a full email address to a coarse ISP bucket.

    Args:
        email: A full email address, e.g. ``"lead@gmail.com"``.

    Returns:
        One of ``"Gmail"``, ``"Yahoo"``, ``"Outlook"``, ``"Apple"``, ``"Other"``.
    """
    if "@" not in email:
        return "Other"
    domain = email.rsplit("@", 1)[-1]
    return classify_isp_from_domain(domain)


def classify_domain_two_axis(domain: str) -> tuple[str, str]:
    """Return ``(mailbox_provider, reputation_group)`` for a domain.

    Both values are lowercase strings.  Falls back to ``('other', 'other')``
    for any domain not present in the seed classification table.

    Normalization applied: lowercase, strip leading/trailing whitespace, strip
    trailing dot.  No glob or prefix matching is performed — only exact domain
    lookups against the seed set are valid; that avoids false positives from
    yahoo.* prefix matching (see task notes).

    Args:
        domain: Email domain (the part after ``@``), e.g. ``"Gmail.COM "``.

    Returns:
        A ``(mailbox_provider, reputation_group)`` tuple of lowercase strings.
    """
    normalized = (domain or "").strip().lower().rstrip(".")
    return _DOMAIN_TWO_AXIS.get(normalized, ("other", "other"))


def classify_isp_from_cloudwatch_label(label: str) -> str:
    """Map a CloudWatch `isp` dimension value (e.g. ``"GMAIL"``) to the same
    bucket names used for our own DB-derived stats, so the two data sources
    line up under one key in the reputation dashboard.

    Args:
        label: The raw `isp` dimension value from an
            `AWS/SES` `Reputation.IspFeedback` metric.

    Returns:
        One of ``"Gmail"``, ``"Yahoo"``, ``"Outlook"``, ``"Apple"``, ``"Other"``.
    """
    normalized = (label or "").strip().lower()

    if "gmail" in normalized:
        return "Gmail"
    if "yahoo" in normalized:
        return "Yahoo"
    if any(marker in normalized for marker in ("outlook", "hotmail", "live", "msn")):
        return "Outlook"
    if any(marker in normalized for marker in ("icloud", "apple")):
        return "Apple"
    return "Other"
