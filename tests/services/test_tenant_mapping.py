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


_VALID_CONSENT = (
    "consent:\n"
    "  channels: [1]\n"
    "  source_label: acme\n"
    "  text: {from: t}\n"
    "  captured_at: {from: c}\n"
)


def test_fields_wrong_type_raises(tmp_path: Path):
    """Finding 1: `fields:` as a list, not a mapping, must not hit AttributeError."""
    (tmp_path / "acme.yaml").write_text(
        "tenant: acme\nidentity:\n  email: email\nfields:\n  - foo\n" + _VALID_CONSENT,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="fields"):
        load_tenant_mapping("acme", mappings_dir=tmp_path)


def test_invalid_yaml_syntax_raises(tmp_path: Path):
    """Finding 2: a syntactically broken file must not leak yaml.YAMLError."""
    (tmp_path / "acme.yaml").write_text(
        "tenant: acme\nidentity: [unbalanced\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid YAML"):
        load_tenant_mapping("acme", mappings_dir=tmp_path)


def test_repeating_wrong_type_raises(tmp_path: Path):
    """Finding 3: `repeating:` as a list, not a mapping, must not hit AttributeError."""
    (tmp_path / "acme.yaml").write_text(
        "tenant: acme\nidentity:\n  email: email\nfields: {}\nrepeating:\n  - foo\n"
        + _VALID_CONSENT,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="repeating"):
        load_tenant_mapping("acme", mappings_dir=tmp_path)


def test_derived_wrong_type_raises(tmp_path: Path):
    """Finding 3: `derived:` as a list, not a mapping, must not hit AttributeError."""
    (tmp_path / "acme.yaml").write_text(
        "tenant: acme\nidentity:\n  email: email\nfields: {}\nderived:\n  - foo\n"
        + _VALID_CONSENT,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="derived"):
        load_tenant_mapping("acme", mappings_dir=tmp_path)


def test_consent_channels_non_int_raises(tmp_path: Path):
    """Finding 4: a non-numeric channel value must name consent.channels."""
    (tmp_path / "acme.yaml").write_text(
        "tenant: acme\n"
        "identity:\n  email: email\n"
        "fields: {}\n"
        "consent:\n"
        "  channels: ['one']\n"
        "  source_label: acme\n"
        "  text: {from: t}\n"
        "  captured_at: {from: c}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="consent.channels"):
        load_tenant_mapping("acme", mappings_dir=tmp_path)


def test_repeating_max_non_int_raises(tmp_path: Path):
    """Audit finding: repeating.<name>.max must not raise a bare, unnamed ValueError."""
    (tmp_path / "acme.yaml").write_text(
        "tenant: acme\n"
        "identity:\n  email: email\n"
        "fields: {}\n"
        "repeating:\n"
        "  vehicles:\n"
        "    columns: 'vehicle_{n}_{field}'\n"
        "    fields: [year]\n"
        "    max: many\n"
        + _VALID_CONSENT,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="repeating.vehicles.max"):
        load_tenant_mapping("acme", mappings_dir=tmp_path)


@pytest.mark.parametrize("key", ["text", "captured_at"])
def test_consent_text_or_captured_at_normalize_raises(tmp_path: Path, key: str):
    """Final-review finding: `normalize:` on consent.text/captured_at is
    parsed into a FieldSpec but map_row always hardcodes no-normalizer for
    these two fields, so a tenant author who sets one gets a silent no-op.
    Reject the config outright instead.
    """
    (tmp_path / "acme.yaml").write_text(
        "tenant: acme\n"
        "identity:\n  email: email\n"
        "fields: {}\n"
        "consent:\n"
        "  channels: [1]\n"
        "  source_label: acme\n"
        f"  {key}: {{from: t, normalize: name}}\n"
        + ("  captured_at: {from: c}\n" if key == "text" else "  text: {from: t2}\n"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=f"consent.{key}"):
        load_tenant_mapping("acme", mappings_dir=tmp_path)


def test_returned_fields_mapping_is_immutable():
    """Finding 5: the cache must not hand out mutable shared state."""
    m = load_tenant_mapping("otto")
    with pytest.raises(TypeError):
        m.fields["new_field"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        m.consent.optional["new_key"] = None  # type: ignore[index]
