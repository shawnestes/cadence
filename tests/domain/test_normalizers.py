"""Pure normalizer tests — no DB, no app wiring."""

from __future__ import annotations

import pytest

from app.domain.normalizers import (
    NORMALIZERS,
    normalize_email,
    normalize_name,
    normalize_phone_e164,
    normalize_state,
    normalize_year,
    normalize_zip5,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("(512) 555-1234", "+15125551234"),
        ("512-555-1234", "+15125551234"),
        ("15125551234", "+15125551234"),
        ("+1 512 555 1234", "+15125551234"),
        ("5551234", None),          # 7 digits, no area code
        ("512-555-1234 x22", None), # extension: ambiguous, reject
        ("", None),
        (None, None),
        ("not a phone", None),
    ],
)
def test_normalize_phone_e164(raw, expected):
    assert normalize_phone_e164(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("78701", "78701"),
        ("78701-1234", "78701"),
        ("787011234", "78701"),
        ("1234", "01234"),   # Excel stripped the leading zero
        ("501", "00501"),
        ("  78701  ", "78701"),
        ("", None),
        (None, None),
        ("ABCDE", None),
    ],
)
def test_normalize_zip5(raw, expected):
    assert normalize_zip5(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("TX", "TX"),
        ("tx", "TX"),
        ("Texas", "TX"),
        ("  new york ", "NY"),
        ("DC", "DC"),
        ("ZZ", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_state(raw, expected):
    assert normalize_state(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2019", 2019),
        (2019, 2019),
        (" 2019 ", 2019),
        ("N/A", None),
        ("", None),
        (None, None),
        ("1799", None),   # out of range
        ("2200", None),
    ],
)
def test_normalize_year(raw, expected):
    assert normalize_year(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SHAWN", "Shawn"),
        ("shawn", "Shawn"),
        ("McDonald", "McDonald"),   # mixed case is left alone
        ("  shawn   estes ", "Shawn Estes"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Shawn@Example.COM ", "shawn@example.com"),
        ("shawn@example.com", "shawn@example.com"),
        ("shawn@localhost", None),
        ("shawn", None),
        ("a@b@c.com", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


def test_normalizers_registry_covers_every_yaml_name():
    assert set(NORMALIZERS) == {"email", "name", "state", "zip5", "phone_e164", "year"}
