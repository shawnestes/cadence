"""US state -> IANA timezone mapping and local send-window math (SPEC_ADDENDUM_002 §1.2).

Pure domain logic: no FastAPI, no SQLAlchemy. `timezone_for_state` deliberately
returns `None` for missing/unknown state codes rather than defaulting — the
caller (send worker) is responsible for applying `send_default_timezone`.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

__all__ = ["timezone_for_state", "next_window_open"]

# Split-zone states (part of the state sits in a neighboring zone) map to the
# IANA zone covering the majority of the state's population, per SPEC_ADDENDUM_002
# §1.2 ("dominant zone"). Notable choices:
#   - TX, KS, NE, SD, ND: dominant Central despite western panhandles in Mountain.
#   - FL, IN, KY, MI: dominant Eastern.
#   - TN: dominant Central (Nashville + Memphis metros outweigh Knoxville/Chattanooga).
#   - AZ: does not observe DST; America/Phoenix (no daylight-saving variant needed).
#   - ID, OR: dominant zone Mountain/Pacific respectively (larger population share).
_STATE_TIMEZONES: dict[str, str] = {
    # Eastern
    "CT": "America/New_York",
    "DE": "America/New_York",
    "DC": "America/New_York",
    "FL": "America/New_York",
    "GA": "America/New_York",
    "IN": "America/New_York",
    "KY": "America/New_York",
    "ME": "America/New_York",
    "MD": "America/New_York",
    "MA": "America/New_York",
    "MI": "America/New_York",
    "NH": "America/New_York",
    "NJ": "America/New_York",
    "NY": "America/New_York",
    "NC": "America/New_York",
    "OH": "America/New_York",
    "PA": "America/New_York",
    "RI": "America/New_York",
    "SC": "America/New_York",
    "VT": "America/New_York",
    "VA": "America/New_York",
    "WV": "America/New_York",
    # Central
    "AL": "America/Chicago",
    "AR": "America/Chicago",
    "IL": "America/Chicago",
    "IA": "America/Chicago",
    "KS": "America/Chicago",
    "LA": "America/Chicago",
    "MN": "America/Chicago",
    "MS": "America/Chicago",
    "MO": "America/Chicago",
    "NE": "America/Chicago",
    "ND": "America/Chicago",
    "OK": "America/Chicago",
    "SD": "America/Chicago",
    "TN": "America/Chicago",
    "TX": "America/Chicago",
    "WI": "America/Chicago",
    # Mountain
    "CO": "America/Denver",
    "ID": "America/Denver",
    "MT": "America/Denver",
    "NM": "America/Denver",
    "UT": "America/Denver",
    "WY": "America/Denver",
    "AZ": "America/Phoenix",  # no DST
    # Pacific
    "CA": "America/Los_Angeles",
    "NV": "America/Los_Angeles",
    "OR": "America/Los_Angeles",
    "WA": "America/Los_Angeles",
    # Non-contiguous
    "AK": "America/Anchorage",
    "HI": "Pacific/Honolulu",
    # Territories
    "PR": "America/Puerto_Rico",
    "VI": "America/St_Thomas",
    "GU": "Pacific/Guam",
    "AS": "Pacific/Pago_Pago",
    "MP": "Pacific/Guam",
}


def timezone_for_state(state: str | None) -> str | None:
    """Map a US state/territory code to its dominant IANA timezone name.

    Args:
        state: A two-letter USPS state/territory code (case-insensitive),
            e.g. "TX". Leading/trailing whitespace is tolerated.

    Returns:
        The IANA zone name, or `None` if `state` is missing, blank, or not a
        recognized code. Callers should fall back to a configured default
        timezone (e.g. `settings.send_default_timezone`) in that case — this
        function stays pure and never guesses.
    """
    if not state:
        return None
    return _STATE_TIMEZONES.get(state.strip().upper())


def next_window_open(
    *,
    now_utc: datetime,
    zone_name: str,
    start_hour: int,
    end_hour: int,
) -> datetime:
    """Return the next UTC instant the local send window [start_hour, end_hour) is open.

    Args:
        now_utc: Current instant, as a timezone-aware UTC datetime.
        zone_name: IANA zone name to evaluate the window in.
        start_hour: Local hour the window opens (inclusive), 0-23.
        end_hour: Local hour the window closes (exclusive), 0-23.

    Returns:
        `now_utc` unchanged if it already falls inside the local window;
        otherwise the UTC instant of the next window opening (today or
        tomorrow, in local time).
    """
    zone = ZoneInfo(zone_name)
    local_now = now_utc.astimezone(zone)
    local_date = local_now.date()
    window_start = datetime.combine(local_date, time(hour=start_hour), tzinfo=zone)
    window_end = datetime.combine(local_date, time(hour=end_hour), tzinfo=zone)

    if window_start <= local_now < window_end:
        return now_utc

    if local_now < window_start:
        next_open = window_start
    else:
        next_open = datetime.combine(
            local_date + timedelta(days=1), time(hour=start_hour), tzinfo=zone
        )
    return next_open.astimezone(now_utc.tzinfo)
