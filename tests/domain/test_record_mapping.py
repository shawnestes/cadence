"""Row mapping: raw dict -> MappedRow. Pure, no DB.

OTTO's export is flat and single-vehicle: `make`/`model` columns rather than
indexed `vehicle_{n}_{field}`, and consent arrives as `tcpa_date` / `ip` /
`user_agent`. The mapping's `max: 1` means one vehicle per record by design.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.record_mapping import disclosure_hash, map_row
from app.services.tenant_mapping import load_tenant_mapping

_OTTO = load_tenant_mapping("otto")

_GOOD_ROW = {
    "email": "Lead@Example.com",
    "user_id": "otto-991",
    "first_name": "SHAWN",
    "last_name": "estes",
    "city": "Austin",
    "state": "texas",
    "zip": "78701-1234",
    "phone": "(512) 555-1234",
    "year": "2019",
    "make": "Toyota",
    "model": "Camry",
    "current_company": "Allstate",
    "jornaya_id": "2EAB0F47-BA9A-3840-4593-8EC5D6E057BB",
    "trusted_form_cert": "https://cert.trustedform.com/abc123",
    "tcpa_text": "By submitting you agree to receive emails.",
    "tcpa_date": "2026-01-15T10:30:00Z",
    "consent_url": "https://otto.example/quote",
    "lead_id": "form-7",
    "ip": "203.0.113.9",
    "user_agent": "Mozilla/5.0",
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


def test_vehicle_and_derived_fields():
    r = map_row(_GOOD_ROW, _OTTO).record
    assert r.attributes["vehicles"] == [
        {"year": 2019, "make": "Toyota", "model": "Camry"},
    ]
    assert r.attributes["vehicle_count"] == 1
    assert r.attributes["vehicle_makes"] == ["toyota"]
    assert r.attributes["newest_vehicle_year"] == 2019


def test_lead_certification_artifacts_are_retained():
    """Jornaya + TrustedForm are the third-party record of the consent event."""
    r = map_row(_GOOD_ROW, _OTTO).record
    assert r.attributes["jornaya_id"] == "2EAB0F47-BA9A-3840-4593-8EC5D6E057BB"
    assert r.attributes["trusted_form_cert"] == "https://cert.trustedform.com/abc123"
    assert r.attributes["current_company"] == "Allstate"


@pytest.mark.parametrize(
    "column", ["jornaya_id", "trusted_form_cert", "current_company"]
)
def test_optional_lead_fields_absent_is_explicit_none_not_reject(column):
    """An export without certification must still load -- these are optional."""
    m = map_row({**_GOOD_ROW, column: ""}, _OTTO)
    assert m.outcome == "ok"
    assert m.record.attributes[column] is None


def test_absent_address_column_does_not_reject():
    """OTTO's export carries no mailing address; address1 is optional."""
    m = map_row(_GOOD_ROW, _OTTO)
    assert m.outcome == "ok"
    assert m.record.attributes["address1"] is None


def test_jsonl_vehicle_array_is_accepted():
    row = {k: v for k, v in _GOOD_ROW.items() if k not in ("year", "make", "model")}
    row["vehicles"] = [{"year": "2018", "make": "Ford", "model": "F-150"}]
    r = map_row(row, _OTTO).record
    assert r.attributes["vehicles"] == [{"year": 2018, "make": "Ford", "model": "F-150"}]
    assert r.attributes["newest_vehicle_year"] == 2018


def test_no_vehicle_yields_zero_count_and_none_year():
    row = {k: v for k, v in _GOOD_ROW.items() if k not in ("year", "make", "model")}
    r = map_row(row, _OTTO).record
    assert r.attributes["vehicles"] == []
    assert r.attributes["vehicle_count"] == 0
    assert r.attributes["vehicle_makes"] == []
    assert r.attributes["newest_vehicle_year"] is None


def test_unparseable_vehicle_year_does_not_crash_max():
    row = {**_GOOD_ROW, "year": "N/A"}
    r = map_row(row, _OTTO).record
    assert r.attributes["newest_vehicle_year"] is None
    # make/model still present, so the vehicle itself survives.
    assert r.attributes["vehicle_count"] == 1


def test_flat_columns_never_yield_more_than_one_vehicle():
    """`max: 1` guards the flat template: the collector must not re-read
    `make`/`model` per index and emit duplicate vehicles."""
    r = map_row(_GOOD_ROW, _OTTO).record
    assert r.attributes["vehicle_count"] == 1
    assert len(r.attributes["vehicles"]) == 1


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
    m = map_row({**_GOOD_ROW, "tcpa_date": ""}, _OTTO)
    assert m.outcome == "quarantine"


def test_unparseable_consent_timestamp_quarantines():
    m = map_row({**_GOOD_ROW, "tcpa_date": "last tuesday"}, _OTTO)
    assert m.outcome == "quarantine"


def test_consent_timestamp_without_tz_is_treated_as_utc():
    m = map_row({**_GOOD_ROW, "tcpa_date": "2026-01-15 10:30:00"}, _OTTO)
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
