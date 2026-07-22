"""Pure tests for app/domain/timezone.py (state->zone mapping + window math)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.timezone import next_window_open, timezone_for_state


def test_known_state_maps_to_zone():
    assert timezone_for_state("TX") == "America/Chicago"
    assert timezone_for_state("CA") == "America/Los_Angeles"
    assert timezone_for_state("NY") == "America/New_York"


def test_state_lookup_is_case_insensitive_and_trims_whitespace():
    assert timezone_for_state("tx") == "America/Chicago"
    assert timezone_for_state(" TX ") == "America/Chicago"


def test_split_zone_state_uses_dominant_zone():
    # Arizona: dominant zone is Mountain without DST.
    assert timezone_for_state("AZ") == "America/Phoenix"
    # Tennessee: dominant population (Nashville + Memphis) is Central.
    assert timezone_for_state("TN") == "America/Chicago"


def test_unknown_or_missing_state_returns_none():
    assert timezone_for_state(None) is None
    assert timezone_for_state("") is None
    assert timezone_for_state("ZZ") is None


def test_next_window_open_returns_now_when_already_inside_window():
    # 2026-07-21 15:00 UTC == 10:00 America/Chicago (CDT, UTC-5).
    now_utc = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)
    result = next_window_open(
        now_utc=now_utc, zone_name="America/Chicago", start_hour=9, end_hour=20
    )
    assert result == now_utc


def test_next_window_open_before_window_rolls_to_start_hour_same_day():
    # 2026-07-21 06:00 UTC == 01:00 America/Chicago -> next open is 09:00 local same day.
    now_utc = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)
    result = next_window_open(
        now_utc=now_utc, zone_name="America/Chicago", start_hour=9, end_hour=20
    )
    local = result.astimezone(__import__("zoneinfo").ZoneInfo("America/Chicago"))
    assert (local.date(), local.hour, local.minute) == (now_utc.astimezone(
        __import__("zoneinfo").ZoneInfo("America/Chicago")
    ).date(), 9, 0)
    assert result > now_utc


def test_next_window_open_after_window_rolls_to_next_day():
    # 2026-07-21 23:00 America/Chicago local (CDT) is late evening, past end_hour=20.
    local_tz = __import__("zoneinfo").ZoneInfo("America/Chicago")
    now_local = datetime(2026, 7, 21, 23, 0, tzinfo=local_tz)
    now_utc = now_local.astimezone(timezone.utc)
    result = next_window_open(
        now_utc=now_utc, zone_name="America/Chicago", start_hour=9, end_hour=20
    )
    result_local = result.astimezone(local_tz)
    assert result_local.date() == datetime(2026, 7, 22).date()
    assert result_local.hour == 9
    assert result > now_utc


def test_next_window_open_preserves_tzinfo_of_input():
    now_utc = datetime(2026, 7, 21, 23, 0, tzinfo=timezone.utc)
    result = next_window_open(
        now_utc=now_utc, zone_name="America/Los_Angeles", start_hour=9, end_hour=20
    )
    assert result.tzinfo is timezone.utc
