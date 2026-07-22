"""Pure domain→ISP classification for reputation reporting.

No FastAPI/SQLAlchemy imports — this module takes and returns primitives so
it can be unit tested without a DB (see CLAUDE.md "Domain logic is pure").
"""

from __future__ import annotations

__all__ = [
    "classify_isp_from_domain",
    "classify_isp_from_email",
    "classify_isp_from_cloudwatch_label",
]

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
