"""Pure ISO-8601 duration parsing for workflow re-entry cooldowns (SPEC §6.5).

No FastAPI or SQLAlchemy imports — domain modules must stay pure per
CLAUDE.md. Supports the date/time designators we actually use for
cooldowns: PnYnMnD and PT nH nM nS (and combinations thereof), e.g.
"P30D", "P1W", "PT12H", "PT0S". Week form ("PnW") is supported as a
standalone designator per ISO-8601, matching common shorthand.
"""
from __future__ import annotations

import re
from datetime import timedelta

__all__ = ["parse_iso8601_duration", "InvalidDurationError"]


class InvalidDurationError(ValueError):
    """Raised when a string is not a supported ISO-8601 duration."""


# Matches PnYnMnDTnHnMnS or the standalone week form PnW.
_WEEK_RE = re.compile(r"^P(?P<weeks>\d+)W$")
_FULL_RE = re.compile(
    r"^P"
    r"(?:(?P<years>\d+)Y)?"
    r"(?:(?P<months>\d+)M)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?"
    r")?$"
)


def parse_iso8601_duration(value: str) -> timedelta:
    """Parse a supported subset of ISO-8601 durations into a ``timedelta``.

    Supports PnYnMnD date parts (years/months treated as 365/30 days —
    approximate, adequate for cooldown windows), the standalone PnW week
    form, and PT nH nM nS time parts. At least one component must be
    present; a bare "P" or "PT" is rejected.

    Args:
        value: The duration string, e.g. "P30D", "P1W", "PT0S", "PT12H".

    Returns:
        The equivalent ``timedelta``.

    Raises:
        InvalidDurationError: If ``value`` is not a well-formed, supported
            ISO-8601 duration.
    """
    if not isinstance(value, str) or not value:
        raise InvalidDurationError(f"duration must be a non-empty string, got {value!r}")

    week_match = _WEEK_RE.match(value)
    if week_match:
        return timedelta(weeks=int(week_match.group("weeks")))

    match = _FULL_RE.match(value)
    if not match or not any(match.groupdict().values()):
        raise InvalidDurationError(f"unsupported ISO-8601 duration: {value!r}")

    parts = {k: int(v) for k, v in match.groupdict().items() if v is not None}
    days = parts.get("years", 0) * 365 + parts.get("months", 0) * 30 + parts.get("days", 0)
    return timedelta(
        days=days,
        hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )
