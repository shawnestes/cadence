"""Integration tests for segment materialization (_materialize_segments) and
segment loading (load_segments).

Scenarios covered:
  1  — contact outside window is excluded from segment membership
  14 — load_segments detects a changed spec_hash and logs segment_loader.spec_hash_changed
  16 — suppressed contact is absent from segment_member_current
  17 — consent_state != 1 (opted out) → sendable=false → absent from segment_member_current
  19 — contact ages out of window → removed_at set on segment_member row
  20 — re-qualification after removal → new segment_member row inserted

All DB access uses raw SQL via conn.execute(text(...)); no ORM models for
segment-related assertions.

Test DB is the session-scoped Docker Postgres spun up by conftest.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text

from app.core.db import async_session_factory
from app.workers.profile_refresh import _materialize_segments

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT = "otto"
_CHANNEL = 1  # email

# Mailbox provider codes (from app.domain.isp.MAILBOX_PROVIDER_CODES:
# gmail=1, microsoft=2, yahoo=3, cable=4, apple=5, corporate=6, other=0)
_MBP_YAHOO = 3

# Engagement tier codes (from engagement_v2.py)
_TIER_ACTIVE = 1
_TIER_COLD = 5

# ---------------------------------------------------------------------------
# Seed helpers (raw SQL, no ORM)
# ---------------------------------------------------------------------------


def _uid() -> str:
    return str(uuid.uuid4())


async def _seed_recipient(conn: Any) -> str:
    """Insert a recipient row and return its UUID string."""
    rid = _uid()
    now = datetime.now(timezone.utc)
    await conn.execute(
        text("""
            INSERT INTO recipients
                (id, tenant, email, external_id, vertical,
                 first_seen_at, last_seen_at, consent_source, consent_at, attributes)
            VALUES
                (:id, :tenant, :email, :ext_id, 'auto',
                 :now, :now, 'otto_form', :now, '{}')
        """),
        {
            "id": rid,
            "tenant": _TENANT,
            "email": f"seg-mat-{rid}@cadencetests.io",
            "ext_id": f"ext-{rid}",
            "now": now,
        },
    )
    await conn.commit()
    return rid


async def _seed_consent_proof(conn: Any, contact_id: str) -> str:
    """Insert a consent_proof row and return its UUID string."""
    proof_id = _uid()
    await conn.execute(
        text("""
            INSERT INTO consent_proof
                (consent_proof_id, tenant, contact_id, captured_at,
                 disclosure_text_hash, channels)
            VALUES
                (:id, :tenant, :contact_id, now(), 'deadbeef', ARRAY[1]::smallint[])
        """),
        {"id": proof_id, "tenant": _TENANT, "contact_id": contact_id},
    )
    await conn.commit()
    return proof_id


async def _seed_contact_profile(
    conn: Any,
    contact_id: str,
    proof_id: str,
    *,
    mailbox_provider: int = _MBP_YAHOO,
    engagement_tier: int = _TIER_ACTIVE,
    last_open_at: datetime | None = None,
    consent_state: int = 1,
    suppressed_at: datetime | None = None,
) -> None:
    """Insert (or upsert) a contact_profile row with the given field values."""
    await conn.execute(
        text("""
            INSERT INTO contact_profile
                (contact_id, tenant, channel,
                 mailbox_provider, engagement_tier,
                 consent_state, consent_proof_id,
                 last_open_at, suppressed_at,
                 profile_version, tier_config_version, updated_at)
            VALUES
                (:cid, :tenant, :channel,
                 :mbp, :tier,
                 :consent_state, :proof_id,
                 :last_open_at, :suppressed_at,
                 1, 'test-config-v1', now())
            ON CONFLICT (tenant, contact_id, channel) DO UPDATE SET
                mailbox_provider    = EXCLUDED.mailbox_provider,
                engagement_tier     = EXCLUDED.engagement_tier,
                consent_state       = EXCLUDED.consent_state,
                last_open_at        = EXCLUDED.last_open_at,
                suppressed_at       = EXCLUDED.suppressed_at,
                updated_at          = now()
        """),
        {
            "cid": contact_id,
            "tenant": _TENANT,
            "channel": _CHANNEL,
            "mbp": mailbox_provider,
            "tier": engagement_tier,
            "consent_state": consent_state,
            "proof_id": proof_id,
            "last_open_at": last_open_at,
            "suppressed_at": suppressed_at,
        },
    )
    await conn.commit()


async def _seed_segment_definition(
    conn: Any,
    *,
    key: str,
    predicate: dict,
    spec_hash: str | None = None,
    acknowledges_cold: bool = True,
) -> str:
    """Insert a segment_definition and return its segment_id."""
    import hashlib

    if spec_hash is None:
        spec_hash = hashlib.sha256(
            json.dumps(predicate, sort_keys=True).encode()
        ).hexdigest()

    seg_id = _uid()
    await conn.execute(
        text("""
            INSERT INTO segment_definition
                (segment_id, tenant, key, name, channel, predicate, spec_hash,
                 active, acknowledges_cold, include_unsendable, updated_at)
            VALUES
                (:seg_id, :tenant, :key, :name, :channel,
                 CAST(:predicate AS jsonb), :spec_hash,
                 true, :acknowledges_cold, false, now())
            ON CONFLICT (tenant, key) DO UPDATE SET
                predicate        = EXCLUDED.predicate,
                spec_hash        = EXCLUDED.spec_hash,
                active           = true,
                acknowledges_cold = EXCLUDED.acknowledges_cold,
                updated_at       = now()
        """),
        {
            "seg_id": seg_id,
            "tenant": _TENANT,
            "key": key,
            "name": key,
            "channel": _CHANNEL,
            "predicate": json.dumps(predicate),
            "spec_hash": spec_hash,
            "acknowledges_cold": acknowledges_cold,
        },
    )
    await conn.commit()

    # Fetch the real segment_id (ON CONFLICT may have kept the existing one).
    row = await conn.execute(
        text(
            "SELECT segment_id FROM segment_definition "
            "WHERE tenant = :tenant AND key = :key"
        ),
        {"tenant": _TENANT, "key": key},
    )
    return str(row.scalar_one())


# ---------------------------------------------------------------------------
# Scenario 1 — contact outside decay window is excluded
# ---------------------------------------------------------------------------


async def test_scenario_1_within_decay():
    """Scenario 1: a contact whose last_open_at is 35 days ago is NOT added to
    a yahoo_opened_30d segment (within 30d predicate).
    """
    now = datetime.now(timezone.utc)
    seg_key = f"test-yahoo-30d-{uuid.uuid4().hex[:8]}"
    predicate = {
        "all": [
            {"field": "mailbox_provider", "op": "eq", "value": "yahoo"},
            {"field": "last_open_at", "op": "within", "value": "30d"},
        ]
    }

    # Seed in separate sessions.
    async with async_session_factory() as conn:
        rid = await _seed_recipient(conn)
        proof_id = await _seed_consent_proof(conn, rid)
        await _seed_contact_profile(
            conn,
            rid,
            proof_id,
            mailbox_provider=_MBP_YAHOO,
            engagement_tier=_TIER_ACTIVE,
            last_open_at=now - timedelta(days=35),
        )
        await _seed_segment_definition(conn, key=seg_key, predicate=predicate)

    # Materialize in a fresh session.
    async with async_session_factory() as conn:
        await _materialize_segments(conn)
        await conn.commit()

    # Verify in a fresh session.
    async with async_session_factory() as conn:
        row = await conn.execute(
            text(
                "SELECT COUNT(*) FROM segment_member sm "
                "JOIN segment_definition sd ON sd.segment_id = sm.segment_id "
                "WHERE sd.key = :key AND sd.tenant = :tenant "
                "  AND sm.contact_id = :cid AND sm.removed_at IS NULL"
            ),
            {"key": seg_key, "tenant": _TENANT, "cid": rid},
        )
        count = row.scalar()

    assert count == 0, (
        f"Expected contact to be absent from segment (35d > 30d window), got count={count}"
    )


# ---------------------------------------------------------------------------
# Scenario 14 — load_segments detects spec_hash change
# ---------------------------------------------------------------------------


async def test_scenario_14_spec_hash_changed():
    """Scenario 14: load_segments detects a changed spec_hash and logs
    segment_loader.spec_hash_changed.
    """
    import hashlib
    import tempfile

    seg_key = f"test-hash-change-{uuid.uuid4().hex[:8]}"
    original_predicate = {"all": [{"field": "mailbox_provider", "op": "eq", "value": "yahoo"}]}
    original_hash = hashlib.sha256(
        json.dumps(original_predicate, sort_keys=True).encode()
    ).hexdigest()

    # Seed the segment with the original hash.
    async with async_session_factory() as conn:
        await _seed_segment_definition(
            conn,
            key=seg_key,
            predicate=original_predicate,
            spec_hash=original_hash,
        )

    # Build a temp YAML dir with a changed predicate to trigger hash mismatch.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        yaml_content = f"""segments:
  - key: {seg_key}
    name: test-segment-changed
    channel: email
    tenant: {_TENANT}
    predicate:
      all:
        - field: mailbox_provider
          op: eq
          value: gmail
"""
        (tmp_path / "test.yaml").write_text(yaml_content)

        from app.services.segment_loader import load_segments

        captured_events: list[str] = []

        class _CapturingProcessor:
            """structlog processor that captures event names."""
            def __call__(self, logger: Any, method: str, event_dict: dict) -> dict:
                captured_events.append(event_dict.get("event", ""))
                raise structlog.DropEvent()

        old_processors = structlog.get_config()["processors"]
        structlog.configure(processors=[_CapturingProcessor()])
        try:
            async with async_session_factory() as conn:
                await load_segments(conn, segments_dir=tmp_path)
        except Exception:
            pass
        finally:
            structlog.configure(processors=old_processors)

    assert "segment_loader.spec_hash_changed" in captured_events, (
        f"Expected 'segment_loader.spec_hash_changed' in captured events; "
        f"got: {captured_events}"
    )


# ---------------------------------------------------------------------------
# Scenario 16 — suppressed contact absent from segment_member_current
# ---------------------------------------------------------------------------


async def test_scenario_16_suppressed_excluded_from_view():
    """Scenario 16: a contact in segment_member with removed_at IS NULL but
    suppressed_at set in contact_profile is absent from segment_member_current
    (because sendable = false when suppressed_at IS NOT NULL).
    """
    seg_key = f"test-suppressed-{uuid.uuid4().hex[:8]}"
    predicate = {"all": [{"field": "mailbox_provider", "op": "eq", "value": "yahoo"}]}

    async with async_session_factory() as conn:
        rid = await _seed_recipient(conn)
        proof_id = await _seed_consent_proof(conn, rid)
        # Insert with consent_state=1 (sendable), no suppression yet.
        await _seed_contact_profile(
            conn,
            rid,
            proof_id,
            mailbox_provider=_MBP_YAHOO,
            engagement_tier=_TIER_ACTIVE,
        )
        seg_id = await _seed_segment_definition(
            conn, key=seg_key, predicate=predicate, acknowledges_cold=True
        )

    # Manually insert a segment_member row as if the contact was active.
    async with async_session_factory() as conn:
        await conn.execute(
            text("""
                INSERT INTO segment_member
                    (segment_id, contact_id, tenant, channel, added_at)
                VALUES (:seg_id, :cid, :tenant, :channel, now())
            """),
            {
                "seg_id": seg_id,
                "cid": rid,
                "tenant": _TENANT,
                "channel": _CHANNEL,
            },
        )
        await conn.commit()

    # Now suppress the contact.
    async with async_session_factory() as conn:
        await conn.execute(
            text("""
                UPDATE contact_profile
                SET suppressed_at = now()
                WHERE tenant = :tenant AND contact_id = :cid AND channel = :channel
            """),
            {"tenant": _TENANT, "cid": rid, "channel": _CHANNEL},
        )
        await conn.commit()

    # Query segment_member_current — contact must be absent.
    async with async_session_factory() as conn:
        row = await conn.execute(
            text(
                "SELECT COUNT(*) FROM segment_member_current "
                "WHERE segment_id = :seg_id AND contact_id = :cid"
            ),
            {"seg_id": seg_id, "cid": rid},
        )
        count = row.scalar()

    assert count == 0, (
        f"Suppressed contact should be absent from segment_member_current, got count={count}"
    )


# ---------------------------------------------------------------------------
# Scenario 17 — opted-out (consent_state=2) contact absent from view
# ---------------------------------------------------------------------------


async def test_scenario_17_opted_out_absent_from_view():
    """Scenario 17: a contact with consent_state=2 (opted out) has sendable=false
    and is absent from segment_member_current.
    """
    seg_key = f"test-optout-{uuid.uuid4().hex[:8]}"
    predicate = {"all": [{"field": "mailbox_provider", "op": "eq", "value": "yahoo"}]}

    async with async_session_factory() as conn:
        rid = await _seed_recipient(conn)
        proof_id = await _seed_consent_proof(conn, rid)
        # consent_state=2 means opted-out; sendable generated col = false
        await _seed_contact_profile(
            conn,
            rid,
            proof_id,
            mailbox_provider=_MBP_YAHOO,
            engagement_tier=_TIER_ACTIVE,
            consent_state=2,
        )
        seg_id = await _seed_segment_definition(
            conn, key=seg_key, predicate=predicate, acknowledges_cold=True
        )

    # Manually insert a segment_member row (simulating a prior membership).
    async with async_session_factory() as conn:
        await conn.execute(
            text("""
                INSERT INTO segment_member
                    (segment_id, contact_id, tenant, channel, added_at)
                VALUES (:seg_id, :cid, :tenant, :channel, now())
            """),
            {
                "seg_id": seg_id,
                "cid": rid,
                "tenant": _TENANT,
                "channel": _CHANNEL,
            },
        )
        await conn.commit()

    # segment_member_current joins contact_profile and filters sendable=true.
    # A contact with consent_state=2 has sendable=false → excluded.
    async with async_session_factory() as conn:
        row = await conn.execute(
            text(
                "SELECT COUNT(*) FROM segment_member_current "
                "WHERE segment_id = :seg_id AND contact_id = :cid"
            ),
            {"seg_id": seg_id, "cid": rid},
        )
        count = row.scalar()

        # Also confirm the sendable column is false.
        sendable_row = await conn.execute(
            text(
                "SELECT sendable FROM contact_profile "
                "WHERE tenant = :tenant AND contact_id = :cid AND channel = :channel"
            ),
            {"tenant": _TENANT, "cid": rid, "channel": _CHANNEL},
        )
        sendable = sendable_row.scalar()

    assert count == 0, (
        f"Opted-out contact (consent_state=2) should be absent from "
        f"segment_member_current, got count={count}"
    )
    assert sendable is False, f"Expected sendable=False for consent_state=2, got {sendable}"


# ---------------------------------------------------------------------------
# Scenario 19 — contact ages out of window → removed_at set
# ---------------------------------------------------------------------------


async def test_scenario_19_aged_out_gets_removed_at():
    """Scenario 19: a contact who was previously in a within-30d segment but
    whose last_open_at is now 35 days old gets removed_at set by _materialize_segments.
    """
    now = datetime.now(timezone.utc)
    seg_key = f"test-aged-out-{uuid.uuid4().hex[:8]}"
    predicate = {
        "all": [
            {"field": "mailbox_provider", "op": "eq", "value": "yahoo"},
            {"field": "last_open_at", "op": "within", "value": "30d"},
        ]
    }

    async with async_session_factory() as conn:
        rid = await _seed_recipient(conn)
        proof_id = await _seed_consent_proof(conn, rid)
        # Contact already aged out — last_open_at 35 days ago.
        await _seed_contact_profile(
            conn,
            rid,
            proof_id,
            mailbox_provider=_MBP_YAHOO,
            engagement_tier=_TIER_ACTIVE,
            last_open_at=now - timedelta(days=35),
        )
        seg_id = await _seed_segment_definition(
            conn, key=seg_key, predicate=predicate, acknowledges_cold=True
        )

    # Simulate a pre-existing active membership (added when the contact was fresh).
    async with async_session_factory() as conn:
        await conn.execute(
            text("""
                INSERT INTO segment_member
                    (segment_id, contact_id, tenant, channel, added_at)
                VALUES (:seg_id, :cid, :tenant, :channel, now() - interval '5 days')
            """),
            {
                "seg_id": seg_id,
                "cid": rid,
                "tenant": _TENANT,
                "channel": _CHANNEL,
            },
        )
        await conn.commit()

    # Materialize — contact is outside window, should get removed_at set.
    async with async_session_factory() as conn:
        await _materialize_segments(conn)
        await conn.commit()

    # The contact no longer qualifies → removed_at should now be set.
    async with async_session_factory() as conn:
        row = await conn.execute(
            text(
                "SELECT removed_at FROM segment_member "
                "WHERE segment_id = :seg_id AND contact_id = :cid"
            ),
            {"seg_id": seg_id, "cid": rid},
        )
        member = row.fetchone()

    assert member is not None, "segment_member row should still exist"
    assert member.removed_at is not None, (
        "Expected removed_at to be set for aged-out contact"
    )


# ---------------------------------------------------------------------------
# Scenario 20 — re-qualification after removal inserts a new row
# ---------------------------------------------------------------------------


async def test_scenario_20_requalification_inserts_new_row():
    """Scenario 20: a contact previously removed (removed_at set) re-qualifies
    for the segment.  The new segment_member row has a new added_at while the
    prior row retains its removed_at.

    _materialize_segments drives the re-qualification detection (computing
    new_ids = current_ids - active previous_ids).  The INSERT in
    profile_refresh.py already uses ``cid::uuid`` so no patching is needed.
    """
    now = datetime.now(timezone.utc)
    seg_key = f"test-requaly-{uuid.uuid4().hex[:8]}"
    predicate = {
        "all": [
            {"field": "mailbox_provider", "op": "eq", "value": "yahoo"},
            {"field": "last_open_at", "op": "within", "value": "30d"},
        ]
    }

    async with async_session_factory() as conn:
        rid = await _seed_recipient(conn)
        proof_id = await _seed_consent_proof(conn, rid)
        # Contact currently qualifies — last opened 5 days ago.
        await _seed_contact_profile(
            conn,
            rid,
            proof_id,
            mailbox_provider=_MBP_YAHOO,
            engagement_tier=_TIER_ACTIVE,
            last_open_at=now - timedelta(days=5),
        )
        seg_id = await _seed_segment_definition(
            conn, key=seg_key, predicate=predicate, acknowledges_cold=True
        )

    # Insert a prior segment_member row with removed_at already set
    # (simulates a previous removal).
    old_added_at = now - timedelta(days=40)
    old_removed_at = now - timedelta(days=35)
    async with async_session_factory() as conn:
        await conn.execute(
            text("""
                INSERT INTO segment_member
                    (segment_id, contact_id, tenant, channel, added_at, removed_at)
                VALUES (:seg_id, :cid, :tenant, :channel, :added_at, :removed_at)
            """),
            {
                "seg_id": seg_id,
                "cid": rid,
                "tenant": _TENANT,
                "channel": _CHANNEL,
                "added_at": old_added_at,
                "removed_at": old_removed_at,
            },
        )
        await conn.commit()

    async with async_session_factory() as conn:
        await _materialize_segments(conn)
        await conn.commit()

    # Verify: two rows should now exist.  The old removed one and a new active one.
    async with async_session_factory() as conn:
        rows_result = await conn.execute(
            text(
                "SELECT added_at, removed_at FROM segment_member "
                "WHERE segment_id = :seg_id AND contact_id = :cid "
                "ORDER BY added_at"
            ),
            {"seg_id": seg_id, "cid": rid},
        )
        rows = rows_result.fetchall()

    assert len(rows) == 2, (
        f"Expected 2 segment_member rows (old + new), got {len(rows)}"
    )

    # First row: the prior removal — removed_at must still be set.
    assert rows[0].removed_at is not None, (
        "Prior row should retain its removed_at"
    )

    # Second row: the new re-qualification — removed_at must be NULL.
    assert rows[1].removed_at is None, (
        "New row after re-qualification should have removed_at=NULL"
    )
    assert rows[1].added_at > old_added_at, (
        "New row's added_at should be more recent than the prior row"
    )


# ---------------------------------------------------------------------------
# CI guard — real YAML files must normalize and compile without error
# ---------------------------------------------------------------------------


def test_real_yaml_segments_normalize_and_compile() -> None:
    """Every segment in the repo YAML files must parse, normalize, and compile.

    This is a pure-unit guard (no DB) that catches YAML predicate structure
    bugs (e.g. sibling all+none keys) before they crash _materialize_segments
    at runtime.
    """
    import yaml
    from app.core.config import get_engagement_config
    from app.domain.segment import normalize
    from app.services.segment_query import compile_segment

    segments_dir = Path(__file__).parents[2] / "segments"
    assert segments_dir.is_dir(), f"segments/ directory not found at {segments_dir}"

    cfg = get_engagement_config()
    errors: list[str] = []

    for yaml_path in sorted(segments_dir.rglob("*.yaml")):
        with yaml_path.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        for entry in doc.get("segments", []) or []:
            key = entry.get("key", "<unknown>")
            predicate = entry.get("predicate") or {}
            acknowledges_cold = bool(entry.get("acknowledges_cold", False))
            include_unsendable = bool(entry.get("include_unsendable", False))
            try:
                normalize(predicate)
                compile_segment(
                    predicate,
                    cfg,
                    acknowledges_cold=acknowledges_cold,
                    include_unsendable=include_unsendable,
                )
            except (ValueError, KeyError) as exc:
                errors.append(f"{yaml_path.relative_to(segments_dir)!s}:{key}: {exc}")

    assert not errors, (
        "The following repo segments failed normalize/compile:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )
