"""The dedup index is what makes a full re-import safe (spec §7)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.core.db import async_session_factory
from app.models.base import uuid7

_TENANT = "otto"


@pytest.mark.asyncio
async def test_duplicate_consent_proof_is_deduped():
    contact_id = uuid7()
    captured_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with async_session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO recipients (id, tenant, email, vertical, first_seen_at,"
                " last_seen_at, consent_source, consent_at)"
                " VALUES (:id, :t, :e, 'auto', now(), now(), 'otto_tcpa', now())"
            ),
            {"id": str(contact_id), "t": _TENANT, "e": f"dedup-{uuid.uuid4()}@cadencetests.io"},
        )
        insert = text(
            "INSERT INTO consent_proof (consent_proof_id, tenant, contact_id, captured_at,"
            " disclosure_text_hash, channels)"
            " VALUES (:id, :t, :c, :cap, :h, ARRAY[1]::smallint[])"
            " ON CONFLICT (tenant, contact_id, disclosure_text_hash, captured_at) DO NOTHING"
        )
        params = {"t": _TENANT, "c": str(contact_id), "cap": captured_at, "h": "abc123"}
        await s.execute(insert, {**params, "id": str(uuid7())})
        await s.execute(insert, {**params, "id": str(uuid7())})
        await s.commit()

        count = (
            await s.execute(
                text(
                    "SELECT count(*) FROM consent_proof"
                    " WHERE tenant = :t AND contact_id = :c"
                ),
                {"t": _TENANT, "c": str(contact_id)},
            )
        ).scalar_one()
        await s.execute(
            text("DELETE FROM consent_proof WHERE contact_id = :c"), {"c": str(contact_id)}
        )
        await s.execute(text("DELETE FROM recipients WHERE id = :c"), {"c": str(contact_id)})
        await s.commit()

    assert count == 1
