"""Pure field normalizers for tenant record ingestion.

Every normalizer accepts whatever a CSV/JSONL cell produced (str, int, None)
and returns a canonical value or None. None means "this cell held nothing
usable" — callers decide whether that is fatal for the row.

No FastAPI, no SQLAlchemy: these are unit-testable without a DB.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

_NON_DIGITS = re.compile(r"\D+")
_WHITESPACE = re.compile(r"\s+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

_MIN_YEAR = 1900
_MAX_YEAR = 2100

_STATE_ABBREVS: frozenset[str] = frozenset(
    """AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN
    MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV
    WI WY PR VI GU AS MP""".split()
)

_STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR",
}


def _text(value: object) -> str | None:
    if value is None:
        return None
    s = _WHITESPACE.sub(" ", str(value)).strip()
    return s or None


def normalize_email(value: object) -> str | None:
    s = _text(value)
    if s is None:
        return None
    s = s.lower()
    return s if _EMAIL_RE.match(s) else None


def normalize_name(value: object) -> str | None:
    """Collapse whitespace; case-correct only all-upper or all-lower input.

    "McDonald" and "van der Berg" are left alone — title-casing them destroys
    information that the source already got right.
    """
    s = _text(value)
    if s is None:
        return None
    if s.isupper() or s.islower():
        return s.title()
    return s


def normalize_state(value: object) -> str | None:
    s = _text(value)
    if s is None:
        return None
    upper = s.upper()
    if upper in _STATE_ABBREVS:
        return upper
    return _STATE_NAMES.get(s.lower())


def normalize_zip5(value: object) -> str | None:
    """Return a 5-digit ZIP.

    Handles ZIP+4 ("78701-1234", "787011234") and spreadsheet-stripped leading
    zeros ("1234" -> "01234").
    """
    s = _text(value)
    if s is None:
        return None
    digits = _NON_DIGITS.sub("", s)
    if len(digits) == 9:
        digits = digits[:5]
    if 3 <= len(digits) <= 5:
        return digits.zfill(5)
    return None


def normalize_phone_e164(value: object) -> str | None:
    """US 10-digit numbers only, rendered +1XXXXXXXXXX.

    An 11-digit number leading with 1 is accepted. Anything else — 7-digit
    local numbers, extensions, international — is rejected rather than guessed.
    """
    s = _text(value)
    if s is None:
        return None
    digits = _NON_DIGITS.sub("", s)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        return None
    return f"+1{digits}"


def normalize_year(value: object) -> int | None:
    """Whole calendar years only.

    Rejects non-finite floats ("inf", "nan", overflowing strings like
    "1e400") and fractional years ("2019.7") rather than truncating them —
    both indicate corrupted source data, not a usable year.
    """
    s = _text(value)
    if s is None:
        return None
    try:
        year_float = float(s)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(year_float):
        return None
    year = int(year_float)
    if year != year_float:
        return None
    return year if _MIN_YEAR <= year <= _MAX_YEAR else None


NORMALIZERS: dict[str, Callable[[object], object]] = {
    "email": normalize_email,
    "name": normalize_name,
    "state": normalize_state,
    "zip5": normalize_zip5,
    "phone_e164": normalize_phone_e164,
    "year": normalize_year,
}
