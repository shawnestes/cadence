"""Integration test: contact_event → profile_refresh → segment_member_current → send_worker.

Exercises the full M-SEG-7 pipeline end-to-end against the test database.

Two scenarios:
  test_segment_path_send_dispatched
    — profile_refresh.run_incremental() materializes segment membership;
      send_worker dispatches the send via the noop ESP; send status = 'sent'.

  test_segment_path_contact_not_in_view_fails_closed
    — a send for a contact absent from segment_member_current results in
      failure_reason = 'not_in_segment_member_current'.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text

import app.workers.profile_refresh as profile_refresh_mod
from app.adapters.esp.base import OutboundMessage, SendResult
from app.core.db import async_session_factory
from app.models.campaign import Campaign
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.template import Template
from app.workers import profile_refresh, send_worker

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT = "otto"
_CHANNEL = 1  # email
_TIER_ACTIVE = 1


# ---------------------------------------------------------------------------
# Seed helpers (raw SQL; mirrors test_segment_materialize.py patterns)
# ---------------------------------------------------------------------------


def _uid() -> str:
    return str(uuid.uuid4())


async def _seed_recipient(conn: Any, email: str) -> str:
    """Insert a recipient and return its UUID string."""
    rid = _uid()
    now = datetime.now(timezone.utc)
    await conn.execute(
        text("""
            INSERT INTO recipients
                (id, tenant, email, external_id, vertical,
                 first_seen_at, last_seen_at, consent_source, consent_at,
                 attributes, engagement_tier)
            VALUES
                (:id, :tenant, :email, :ext_id, 'auto',
                 :now, :now, 'otto_form', :now,
                 '{"first_name": "Test"}'::jsonb, 'engaged')
        """),
        {
            "id": rid,
            "tenant": _TENANT,
            "email": email,
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
    last_open_at: datetime | None = None,
    profile_version: int = 0,
) -> None:
    """Insert a sendable contact_profile row (consent_state=1, no suppression)."""
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
                 3, :tier,
                 1, :proof_id,
                 :last_open_at, NULL,
                 :profile_version, 'test-config-v1', now())
            ON CONFLICT (tenant, contact_id, channel) DO UPDATE SET
                consent_state    = 1,
                last_open_at     = EXCLUDED.last_open_at,
                suppressed_at    = NULL,
                profile_version  = EXCLUDED.profile_version,
                updated_at       = now()
        """),
        {
            "cid": contact_id,
            "tenant": _TENANT,
            "channel": _CHANNEL,
            "tier": _TIER_ACTIVE,
            "proof_id": proof_id,
            "last_open_at": last_open_at,
            "profile_version": profile_version,
        },
    )
    await conn.commit()


async def _seed_segment_definition(
    conn: Any,
    *,
    key: str,
    predicate: dict,
) -> str:
    """Insert an active segment_definition; return its segment_id string."""
    import hashlib

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
                 true, true, false, now())
            ON CONFLICT (tenant, key) DO UPDATE SET
                predicate        = EXCLUDED.predicate,
                spec_hash        = EXCLUDED.spec_hash,
                active           = true,
                acknowledges_cold = true,
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
        },
    )
    await conn.commit()

    # Fetch back the real segment_id (ON CONFLICT may have kept an existing one).
    row = await conn.execute(
        text(
            "SELECT segment_id FROM segment_definition "
            "WHERE tenant = :tenant AND key = :key"
        ),
        {"tenant": _TENANT, "key": key},
    )
    return str(row.scalar_one())


async def _seed_contact_activity_daily(
    conn: Any,
    contact_id: str,
    *,
    opens: int = 1,
) -> None:
    """Insert a contact_activity_daily row for today (gives the incremental pass
    something to aggregate when triggered by a dirty profile_version)."""
    today = datetime.now(timezone.utc).date()
    await conn.execute(
        text("""
            INSERT INTO contact_activity_daily
                (contact_id, tenant, channel, day, sends, delivers,
                 opens, opens_mpp, clicks, clicks_bot, converts,
                 bounces, complaints)
            VALUES
                (:cid, :tenant, :channel, :day, 1, 1,
                 :opens, 0, 0, 0, 0, 0, 0)
            ON CONFLICT (tenant, contact_id, channel, day) DO UPDATE SET
                opens = contact_activity_daily.opens + EXCLUDED.opens
        """),
        {
            "cid": contact_id,
            "tenant": _TENANT,
            "channel": _CHANNEL,
            "day": today,
            "opens": opens,
        },
    )
    await conn.commit()


async def _bump_profile_version(conn: Any, contact_id: str) -> None:
    """Increment profile_version to mark the profile dirty for run_incremental."""
    await conn.execute(
        text("""
            UPDATE contact_profile
            SET profile_version = profile_version + 1,
                updated_at      = now()
            WHERE tenant     = :tenant
              AND contact_id  = :cid
              AND channel     = :channel
        """),
        {"tenant": _TENANT, "cid": contact_id, "channel": _CHANNEL},
    )
    await conn.commit()


async def _seed_campaign_template(
    conn: Any,
    *,
    segment_key: str | None = None,
) -> tuple[str, str]:
    """Insert campaign + template; return (campaign_id, template_id) strings."""
    c_id = _uid()
    t_id = _uid()
    campaign_name = f"test-seg7-campaign-{uuid.uuid4().hex[:8]}"
    cohort_key = f"test-cohort-{uuid.uuid4().hex[:8]}"

    await conn.execute(
        text("""
            INSERT INTO campaigns
                (id, tenant, name, vertical, status, segment_key)
            VALUES
                (:id, :tenant, :name, 'auto', 'active', :segment_key)
        """),
        {
            "id": c_id,
            "tenant": _TENANT,
            "name": campaign_name,
            "segment_key": segment_key,
        },
    )

    await conn.execute(
        text("""
            INSERT INTO templates
                (id, tenant, campaign_id, cohort_key, variant_label,
                 subject, body_html, body_text, status)
            VALUES
                (:id, :tenant, :campaign_id, :cohort_key, 'A',
                 'Hello {{first_name}}',
                 '<p>Hello {{first_name}} <a href="{{unsub_url}}">Unsub</a></p>',
                 'Hello {{first_name}} Unsub: {{unsub_url}}',
                 'approved')
        """),
        {
            "id": t_id,
            "tenant": _TENANT,
            "campaign_id": c_id,
            "cohort_key": cohort_key,
        },
    )
    await conn.commit()
    return c_id, t_id


async def _seed_queued_send(
    conn: Any,
    campaign_id: str,
    template_id: str,
    recipient_id: str,
) -> str:
    """Insert a queued send row with queued_at in the past; return its id."""
    s_id = _uid()
    ancient = datetime(2000, 1, 1, tzinfo=timezone.utc)
    await conn.execute(
        text("""
            INSERT INTO sends
                (id, tenant, campaign_id, template_id, recipient_id,
                 cohort_key, variant_label, esp,
                 rendered_subject, status, queued_at)
            SELECT
                :id, :tenant, :campaign_id, :template_id, :recipient_id,
                t.cohort_key, 'A', 'noop',
                'Hello Test', 'queued', :queued_at
            FROM templates t WHERE t.id = :template_id
        """),
        {
            "id": s_id,
            "tenant": _TENANT,
            "campaign_id": campaign_id,
            "template_id": template_id,
            "recipient_id": recipient_id,
            "queued_at": ancient,
        },
    )
    await conn.commit()
    return s_id


# ---------------------------------------------------------------------------
# Helpers to read back rows
# ---------------------------------------------------------------------------


async def _read_send_status(send_id: str) -> dict[str, Any]:
    async with async_session_factory() as s:
        row = await s.execute(
            text("SELECT status, failure_reason FROM sends WHERE id = :id"),
            {"id": send_id},
        )
        r = row.fetchone()
        if r is None:
            return {}
        return {"status": r.status, "failure_reason": r.failure_reason}


async def _count_in_segment_member_current(
    segment_key: str, contact_id: str
) -> int:
    async with async_session_factory() as s:
        row = await s.execute(
            text(
                "SELECT COUNT(*) FROM segment_member_current "
                "WHERE segment_key = :key AND contact_id = :cid"
            ),
            {"key": segment_key, "cid": contact_id},
        )
        return row.scalar() or 0


async def _count_in_segment_member(
    segment_key: str, contact_id: str
) -> int:
    async with async_session_factory() as s:
        row = await s.execute(
            text(
                "SELECT COUNT(*) FROM segment_member sm "
                "JOIN segment_definition sd ON sd.segment_id = sm.segment_id "
                "WHERE sd.key = :key AND sd.tenant = :tenant "
                "  AND sm.contact_id = :cid AND sm.removed_at IS NULL"
            ),
            {"key": segment_key, "tenant": _TENANT, "cid": contact_id},
        )
        return row.scalar() or 0


# ---------------------------------------------------------------------------
# Capturing noop adapter
# ---------------------------------------------------------------------------


class _CapturingNoopAdapter:
    """Noop ESP that records every OutboundMessage it handles."""

    name = "noop"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(esp_message_id=f"noop-{uuid.uuid4()}")

    async def get_reputation(self):
        raise NotImplementedError

    def parse_webhook(self, payload: dict, signature: str | None) -> list:
        raise NotImplementedError

    async def add_to_esp_suppression(self, email: str, reason: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_segment_path_send_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full pipeline: dirty profile → run_incremental → segment membership →
    send_worker dispatches via noop ESP → send status = 'sent'.
    """
    now = datetime.now(timezone.utc)
    email = f"test-seg7-{uuid.uuid4().hex[:8]}@cadencetests.io"
    seg_key = f"test-seg7-{uuid.uuid4().hex[:8]}"
    predicate = {"all": [{"field": "last_open_at", "op": "within", "value": "30d"}]}

    # --- Step 1: seed recipient, consent_proof, and contact_profile ---
    async with async_session_factory() as conn:
        rid = await _seed_recipient(conn, email)
        proof_id = await _seed_consent_proof(conn, rid)
        await _seed_contact_profile(
            conn,
            rid,
            proof_id,
            last_open_at=now - timedelta(days=5),
            profile_version=0,
        )

    # --- Step 2: seed segment_definition ---
    async with async_session_factory() as conn:
        seg_id = await _seed_segment_definition(
            conn, key=seg_key, predicate=predicate
        )

    # --- Step 3: seed contact_activity_daily + bump profile_version ---
    async with async_session_factory() as conn:
        await _seed_contact_activity_daily(conn, rid, opens=1)
        await _bump_profile_version(conn, rid)

    # Ensure run_incremental will pick up this profile: reset the module-level
    # watermark so it sees the newly-bumped profile_version as dirty.
    monkeypatch.setattr(profile_refresh_mod, "_watermark", 0)

    # --- Step 4: run_incremental and assert segment membership ---
    result = await profile_refresh.run_incremental()
    assert result.get("dirty_count", 0) >= 1, (
        f"Expected at least 1 dirty profile; got {result}"
    )

    member_count = await _count_in_segment_member(seg_key, rid)
    assert member_count == 1, (
        f"Expected contact {rid} in segment_member for {seg_key}; count={member_count}"
    )

    view_count = await _count_in_segment_member_current(seg_key, rid)
    assert view_count == 1, (
        f"Expected contact {rid} in segment_member_current for {seg_key}; count={view_count}"
    )

    # --- Step 5: seed campaign, template, and send ---
    async with async_session_factory() as conn:
        c_id, t_id = await _seed_campaign_template(conn, segment_key=seg_key)
        s_id = await _seed_queued_send(conn, c_id, t_id, rid)

    # --- Step 6: monkeypatch ESP adapter + quiet-hours gate ---
    capturing_adapter = _CapturingNoopAdapter()
    monkeypatch.setattr(send_worker, "get_esp_adapter", lambda identity=None: capturing_adapter)
    monkeypatch.setattr(send_worker, "next_window_open", lambda **kwargs: kwargs["now_utc"])

    # --- Step 7: run_once and assert ---
    run_result = await send_worker.run_once()

    assert run_result["sent"] == 1, (
        f"Expected sent=1; result={run_result}"
    )
    assert len(capturing_adapter.sent) == 1, (
        f"Expected 1 message captured by noop adapter; got {len(capturing_adapter.sent)}"
    )

    send_row = await _read_send_status(s_id)
    assert send_row["status"] == "sent", (
        f"Expected send status='sent'; got {send_row}"
    )


async def test_segment_path_contact_not_in_view_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A send for a contact absent from segment_member_current must fail closed
    with failure_reason = 'not_in_segment_member_current'.
    """
    # Use a different email — this contact will NOT have a profile/segment row.
    email_unsegmented = f"test-seg7-unseg-{uuid.uuid4().hex[:8]}@cadencetests.io"
    seg_key = f"test-seg7-ns-{uuid.uuid4().hex[:8]}"
    predicate = {"all": [{"field": "last_open_at", "op": "within", "value": "30d"}]}

    # Seed recipient — no profile, no segment membership.
    async with async_session_factory() as conn:
        rid = await _seed_recipient(conn, email_unsegmented)

    # Seed segment (so the campaign can reference it) but do NOT materialize
    # membership for this recipient — the contact_profile is deliberately absent.
    async with async_session_factory() as conn:
        await _seed_segment_definition(conn, key=seg_key, predicate=predicate)

    # Seed campaign with segment_key and a queued send for the unsegmented recipient.
    async with async_session_factory() as conn:
        c_id, t_id = await _seed_campaign_template(conn, segment_key=seg_key)
        s_id = await _seed_queued_send(conn, c_id, t_id, rid)

    # Patch quiet-hours gate (capturing adapter is not needed — send must fail
    # before reaching the ESP).
    monkeypatch.setattr(send_worker, "next_window_open", lambda **kwargs: kwargs["now_utc"])

    run_result = await send_worker.run_once()

    assert run_result["failed"] >= 1, (
        f"Expected failed>=1; result={run_result}"
    )

    send_row = await _read_send_status(s_id)
    assert send_row["status"] == "failed", (
        f"Expected send status='failed'; got {send_row}"
    )
    assert send_row["failure_reason"] == "not_in_segment_member_current", (
        f"Expected failure_reason='not_in_segment_member_current'; got {send_row['failure_reason']}"
    )
