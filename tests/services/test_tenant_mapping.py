"""Tenant mapping YAML loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.tenant_mapping import load_tenant_mapping

_REPO_ROOT = Path(__file__).parents[2]


def test_loads_otto_mapping_from_repo_config():
    m = load_tenant_mapping("otto")
    assert m.tenant == "otto"
    assert m.email_source == "email"
    assert m.external_id_source == "user_id"
    assert m.fields["state"].normalize == "state"
    assert m.fields["phone"].required is False
    assert m.consent.required is True
    assert m.consent.channels == (1,)
    assert m.consent.source_label == "otto_tcpa"
    assert m.consent.text.source == "tcpa_text"
    assert m.consent.captured_at.source == "tcpa_consent_at"
    assert "consent_url" == m.consent.optional["source_url"].source


def test_otto_repeating_and_derived():
    m = load_tenant_mapping("otto")
    (vehicles,) = m.repeating
    assert vehicles.name == "vehicles"
    assert vehicles.column_template == "vehicle_{n}_{field}"
    assert vehicles.fields == ("year", "make", "model", "vin")
    assert vehicles.max == 5
    names = {d.name: (d.func, d.source, d.field) for d in m.derived}
    assert names["vehicle_count"] == ("count", "vehicles", None)
    assert names["vehicle_makes"] == ("list", "vehicles", "make")
    assert names["newest_vehicle_year"] == ("max", "vehicles", "year")


def test_unknown_tenant_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="no mapping file"):
        load_tenant_mapping("nosuchtenant", mappings_dir=tmp_path)


def test_unknown_normalizer_raises(tmp_path: Path):
    (tmp_path / "acme.yaml").write_text(
        "tenant: acme\n"
        "identity:\n  email: email\n"
        "fields:\n  city: {from: city, normalize: nonesuch}\n"
        "consent:\n"
        "  channels: [1]\n"
        "  source_label: acme\n"
        "  text: {from: t}\n"
        "  captured_at: {from: c}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown normalizer 'nonesuch'"):
        load_tenant_mapping("acme", mappings_dir=tmp_path)


def test_missing_consent_block_raises(tmp_path: Path):
    (tmp_path / "acme.yaml").write_text(
        "tenant: acme\nidentity:\n  email: email\nfields: {}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="consent"):
        load_tenant_mapping("acme", mappings_dir=tmp_path)
