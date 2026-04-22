"""Pure tests for cohort-key derivation."""

from __future__ import annotations

from app.domain.cohort import compute_cohort_key, derive_recipient_cohort


def test_compute_cohort_key_sorts_dims():
    assert (
        compute_cohort_key("auto", {"state": "TX", "age_band": "35-44"})
        == "auto:35-44:TX"
    )


def test_compute_cohort_key_unknown_falls_to_unknown():
    assert (
        compute_cohort_key("auto", {"state": "TX", "age_band": ""})
        == "auto:unknown:TX"
    )


def test_derive_recipient_cohort():
    assert (
        derive_recipient_cohort("auto", {"state": "TX"}, ["state", "age_band"])
        == "auto:unknown:TX"
    )
