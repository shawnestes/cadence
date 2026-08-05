"""Row mapping: raw dict -> MappedRow. Pure, no DB."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.domain.record_mapping import disclosure_hash, map_row
from app.services.tenant_mapping import load_tenant_mapping

_OTTO = load_tenant_mapping("otto")

_GOOD_ROW = {
    "email": "Lead@Example.com",
    "user_id": "otto-991",
    "first_name": "SHAWN",
    "last_name": "estes",
    "address": "100 Congress Ave",
    "city": "Austin",
    "state": "texas",
    "zip": "78701-1234",
    "phone": "(512) 555-1234",
    "vehicle_1_year": "2019",
    "vehicle_1_make": "Toyota",
    "vehicle_1_model": "Camry",
    "vehicle_2_year": "2021",
    "vehicle_2_make": "HONDA",
    "vehicle_2_model": "CR-V",
    "tcpa_text": "By submitting you agree to receive emails.",
    "tcpa_consent_at": "2026-01-15T10:30:00Z",
    "consent_url": "https://otto.example/quote",
    "lead_id": "form-7",
    "consent_ip": "203.0.113.9",
    "consent_ua": "Mozilla/5.0",
}


def test_good_row_maps_to_ok():
    m = map_row(_GOOD_ROW, _OTTO)
    assert m.outcome == "ok"
    assert m.errors == ()
    r = m.record
    assert r.email == "lead@example.com"
    assert r.external_id == "otto-991"
    assert r.consent_source == "otto_tcpa"
    assert r.attributes["first_name"] == "Shawn"
    assert r.attributes["last_name"] == "Estes"
    assert r.attributes["state"] == "TX"
    assert r.attributes["zip"] == "78701"
    assert r.attributes["phone"] == "+15125551234"
    assert r.attributes["user_id"] == "otto-991"


def test_vehicles_and_derived_fields():
    r = map_row(_GOOD_ROW, _OTTO).record
    assert r.attributes["vehicles"] == [
        {"year": 2019, "make": "Toyota", "model": "Camry"},
        {"year": 2021, "make": "HONDA", "model": "CR-V"},
    ]
    assert r.attributes["vehicle_count"] == 2
    assert r.attributes["vehicle_makes"] == ["toyota", "honda"]
    assert r.attributes["newest_vehicle_year"] == 2021


def test_jsonl_vehicle_array_is_accepted():
    row = {**_GOOD_ROW}
    for k in list(row):
        if k.startswith("vehicle_"):
            del row[k]
    row["vehicles"] = [{"year": "2018", "make": "Ford", "model": "F-150"}]
    r = map_row(row, _OTTO).record
    assert r.attributes["vehicles"] == [{"year": 2018, "make": "Ford", "model": "F-150"}]
    assert r.attributes["newest_vehicle_year"] == 2018


def test_no_vehicles_yields_zero_count_and_none_year():
    row = {k: v for k, v in _GOOD_ROW.items() if not k.startswith("vehicle_")}
    r = map_row(row, _OTTO).record
    assert r.attributes["vehicles"] == []
    assert r.attributes["vehicle_count"] == 0
    assert r.attributes["vehicle_makes"] == []
    assert r.attributes["newest_vehicle_year"] is None


def test_unparseable_vehicle_year_does_not_crash_max():
    row = {**_GOOD_ROW, "vehicle_1_year": "N/A", "vehicle_2_year": ""}
    r = map_row(row, _OTTO).record
    assert r.attributes["newest_vehicle_year"] is None
    assert r.attributes["vehicle_count"] == 2


def test_vehicles_beyond_max_are_dropped():
    row = {**_GOOD_ROW}
    for n in range(3, 9):
        row[f"vehicle_{n}_make"] = f"Make{n}"
    r = map_row(row, _OTTO).record
    assert r.attributes["vehicle_count"] == 5


def test_missing_optional_phone_is_explicit_none():
    row = {**_GOOD_ROW, "phone": ""}
    r = map_row(row, _OTTO).record
    assert "phone" in r.attributes
    assert r.attributes["phone"] is None


def test_bad_email_rejects():
    m = map_row({**_GOOD_ROW, "email": "not-an-email"}, _OTTO)
    assert m.outcome == "reject"
    assert any("email" in e for e in m.errors)
    assert m.record is None


def test_missing_consent_text_quarantines():
    m = map_row({**_GOOD_ROW, "tcpa_text": "   "}, _OTTO)
    assert m.outcome == "quarantine"
    assert any("tcpa_text" in e or "consent" in e for e in m.errors)


def test_missing_consent_timestamp_quarantines():
    m = map_row({**_GOOD_ROW, "tcpa_consent_at": ""}, _OTTO)
    assert m.outcome == "quarantine"


def test_unparseable_consent_timestamp_quarantines():
    m = map_row({**_GOOD_ROW, "tcpa_consent_at": "last tuesday"}, _OTTO)
    assert m.outcome == "quarantine"


def test_consent_timestamp_without_tz_is_treated_as_utc():
    m = map_row({**_GOOD_ROW, "tcpa_consent_at": "2026-01-15 10:30:00"}, _OTTO)
    assert m.record.consent.captured_at == datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc)


def test_missing_required_field_rejects():
    row = {k: v for k, v in _GOOD_ROW.items() if k != "state"}
    m = map_row(row, _OTTO)
    assert m.outcome == "reject"
    assert any("state" in e for e in m.errors)


def test_consent_optional_fields_carry_through():
    c = map_row(_GOOD_ROW, _OTTO).record.consent
    assert c.source_url == "https://otto.example/quote"
    assert c.form_id == "form-7"
    assert c.ip == "203.0.113.9"
    assert c.user_agent == "Mozilla/5.0"
    assert c.channels == (1,)


def test_disclosure_hash_is_whitespace_insensitive_and_stable():
    a = disclosure_hash("By submitting  you agree.\n")
    b = disclosure_hash("By submitting you agree.")
    assert a == b
    assert len(a) == 64
    assert a != disclosure_hash("By submitting you agree to SMS.")
