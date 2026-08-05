"""DB-backed tests for the record ingest service."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.db import async_session_factory
from app.domain.record_mapping import ConsentRecord, NormalizedRecord
from app.services.record_ingest import ingest_record, quarantine_row

_TENANT = "otto"
_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _record(email: str, *, captured_at: datetime, makes: list[str] | None = None):
    return NormalizedRecord(
        email=email,
        external_id="otto-1",
        vertical=None,
        attributes={
            "first_name": "Shawn",
            "state": "TX",
            "zip": "78701",
            "phone": "+15125551234",
            "vehicles": [{"year": 2019, "make": m} for m in (makes or ["Toyota"])],
            "vehicle_count": len(makes or ["Toyota"]),
            "vehicle_makes": [m.lower() for m in (makes or ["Toyota"])],
            "newest_vehicle_year": 2019,
        },
        consent=ConsentRecord(
            text="By submitting you agree to receive emails.",
            captured_at=captured_at,
            channels=(1,),
            source_url="https://otto.example/quote",
            form_id="form-7",
            ip="203.0.113.9",
            user_agent="Mozilla/5.0",
        ),
        consent_source="otto_tcpa",
    )


async def _profile(session, email: str) -> dict | None:
    return (
        await session.execute(
            text(
                "SELECT cp.* FROM contact_profile cp JOIN recipients r"
                " ON r.id = cp.contact_id AND r.tenant = cp.tenant"
                " WHERE cp.tenant = :t AND r.email = :e"
            ),
            {"t": _TENANT, "e": email},
        )
    ).mappings().one_or_none()


@pytest.mark.asyncio
async def test_happy_path_writes_all_three_rows():
    email = f"happy-{uuid.uuid4()}@cadencetests.io"
    async with async_session_factory() as s:
        status, unknown = await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW), now=_NOW,
        )
        await s.commit()
        assert status == "created"

        r = (
            await s.execute(
                text("SELECT * FROM recipients WHERE tenant = :t AND email = :e"),
                {"t": _TENANT, "e": email},
            )
        ).mappings().one()
        assert r["consent_source"] == "otto_tcpa"
        assert r["attributes"]["vehicle_count"] == 1

        proofs = (
            await s.execute(
                text("SELECT * FROM consent_proof WHERE tenant = :t AND contact_id = :c"),
                {"t": _TENANT, "c": str(r["id"])},
            )
        ).mappings().all()
        assert len(proofs) == 1
        assert proofs[0]["raw"]["disclosure_text"].startswith("By submitting")
        assert proofs[0]["channels"] == [1]

        p = await _profile(s, email)
        assert p["consent_state"] == 1
        assert p["sendable"] is True
        assert p["consent_proof_id"] == proofs[0]["consent_proof_id"]
        assert p["email_domain"] == "cadencetests.io"


@pytest.mark.asyncio
async def test_reimport_is_idempotent_and_appends_no_duplicate_proof():
    email = f"reimport-{uuid.uuid4()}@cadencetests.io"
    rec = _record(email, captured_at=_NOW)
    async with async_session_factory() as s:
        first, _ = await ingest_record(s, tenant=_TENANT, vertical="auto", record=rec, now=_NOW)
        await s.commit()
        second, _ = await ingest_record(s, tenant=_TENANT, vertical="auto", record=rec, now=_NOW)
        await s.commit()
        assert (first, second) == ("created", "updated")

        counts = (
            await s.execute(
                text(
                    "SELECT (SELECT count(*) FROM recipients WHERE tenant=:t AND email=:e) AS r,"
                    " (SELECT count(*) FROM consent_proof cp JOIN recipients r"
                    "   ON r.id = cp.contact_id WHERE r.email = :e) AS p"
                ),
                {"t": _TENANT, "e": email},
            )
        ).mappings().one()
        assert counts["r"] == 1
        assert counts["p"] == 1


@pytest.mark.asyncio
async def test_attributes_are_replaced_not_merged():
    email = f"replace-{uuid.uuid4()}@cadencetests.io"
    async with async_session_factory() as s:
        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW, makes=["Toyota", "Honda"]), now=_NOW,
        )
        await s.commit()
        thin = _record(email, captured_at=_NOW + timedelta(days=1), makes=["Ford"])
        thin.attributes["phone"] = None
        await ingest_record(s, tenant=_TENANT, vertical="auto", record=thin, now=_NOW)
        await s.commit()

        attrs = (
            await s.execute(
                text("SELECT attributes FROM recipients WHERE tenant=:t AND email=:e"),
                {"t": _TENANT, "e": email},
            )
        ).scalar_one()
        assert attrs["phone"] is None
        assert attrs["vehicle_count"] == 1
        assert attrs["vehicle_makes"] == ["ford"]


@pytest.mark.asyncio
async def test_suppressed_contact_is_not_resurrected():
    email = f"supp-{uuid.uuid4()}@cadencetests.io"
    async with async_session_factory() as s:
        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW), now=_NOW,
        )
        await s.commit()
        await s.execute(
            text(
                "INSERT INTO suppressions (id, tenant, email, reason, source)"
                " VALUES (gen_random_uuid(), :t, :e, 'unsubscribe', 'one_click')"
            ),
            {"t": _TENANT, "e": email},
        )
        await s.commit()

        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW + timedelta(days=2)), now=_NOW,
        )
        await s.commit()

        p = await _profile(s, email)
        assert p["consent_state"] == 2
        assert p["suppressed_at"] is not None
        assert p["sendable"] is False


@pytest.mark.asyncio
async def test_suppression_ratchet_survives_after_suppressions_row_is_removed():
    """Falsifies a non-ratcheted implementation.

    The previous test leaves the suppressions row in place, so a fresh
    per-call lookup alone (with no ratchet on the contact_profile write)
    would still pass it. This test deletes the suppressions row before the
    final re-import, so only a real ratchet (GREATEST/COALESCE against the
    already-stored row) keeps the profile locked at opted-out.
    """
    email = f"unsupp-{uuid.uuid4()}@cadencetests.io"
    async with async_session_factory() as s:
        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW), now=_NOW,
        )
        await s.commit()
        await s.execute(
            text(
                "INSERT INTO suppressions (id, tenant, email, reason, source)"
                " VALUES (gen_random_uuid(), :t, :e, 'unsubscribe', 'one_click')"
            ),
            {"t": _TENANT, "e": email},
        )
        await s.commit()

        # Lock the profile at consent_state=2 / suppressed_at set.
        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW + timedelta(days=2)), now=_NOW,
        )
        await s.commit()

        # Remove the suppression entirely — a per-call lookup would now see
        # this contact as not suppressed.
        await s.execute(
            text("DELETE FROM suppressions WHERE tenant = :t AND email = :e"),
            {"t": _TENANT, "e": email},
        )
        await s.commit()

        # Re-import with a fresh, opted-in record.
        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW + timedelta(days=4)), now=_NOW,
        )
        await s.commit()

        p = await _profile(s, email)
        assert p["consent_state"] == 2
        assert p["suppressed_at"] is not None
        assert p["sendable"] is False


@pytest.mark.asyncio
async def test_older_consent_does_not_regress_consent_at():
    email = f"older-{uuid.uuid4()}@cadencetests.io"
    newer = _NOW
    older = _NOW - timedelta(days=30)
    async with async_session_factory() as s:
        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=newer), now=_NOW,
        )
        await s.commit()
        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=older), now=_NOW,
        )
        await s.commit()

        p = await _profile(s, email)
        assert p["consent_at"] == newer


@pytest.mark.asyncio
async def test_unknown_domain_is_reported_for_mx_enqueue():
    email = f"unknown-{uuid.uuid4()}@cadencetests.io"
    async with async_session_factory() as s:
        _, unknown = await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW), now=_NOW,
        )
        await s.commit()
    assert unknown == "cadencetests.io"


@pytest.mark.asyncio
async def test_quarantine_row_writes_reason_and_raw():
    async with async_session_factory() as s:
        await quarantine_row(
            s, tenant=_TENANT, reason="missing consent text",
            raw_row={"email": "q@cadencetests.io", "tcpa_text": ""}, now=_NOW,
        )
        await s.commit()
        row = (
            await s.execute(
                text(
                    "SELECT reason, raw, contact_id FROM contact_quarantine"
                    " WHERE tenant = :t AND raw->>'email' = :e"
                ),
                {"t": _TENANT, "e": "q@cadencetests.io"},
            )
        ).mappings().one()
        assert row["contact_id"] is None
        assert row["reason"] == "missing consent text"
        await s.execute(
            text("DELETE FROM contact_quarantine WHERE tenant=:t AND raw->>'email' = :e"),
            {"t": _TENANT, "e": "q@cadencetests.io"},
        )
        await s.commit()
