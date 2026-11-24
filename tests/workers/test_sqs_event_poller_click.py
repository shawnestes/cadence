"""SES Click ingestion against a real DB.

The mocked poller tests cannot catch SQL that Postgres rejects; every Click
event once failed this way and stayed in the queue unrecorded.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.db import async_session_factory
from app.workers.sqs_event_poller import _process_message

_TENANT = "otto_click_test"
_CLICK = 7


@pytest_asyncio.fixture(autouse=True)
async def _clean_rows():
    async def _purge() -> None:
        async with async_session_factory() as s:
            for table in ("contact_event", "contact_activity_daily", "click_registry"):
                await s.execute(text(f"DELETE FROM {table} WHERE tenant = :t"), {"t": _TENANT})
            await s.commit()

    await _purge()
    yield
    await _purge()


def _click_message(email: str, msg_id: str, at: datetime) -> dict:
    ses = {
        "eventType": "Click",
        "mail": {
            "messageId": msg_id,
            "timestamp": at.isoformat().replace("+00:00", "Z"),
            "destination": [email],
            "tags": {"tenant": [_TENANT]},
        },
        "click": {
            "linkUrl": "https://example.com/quote?click_id=ck-123",
            "ipAddress": "203.0.113.9",
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
    }
    envelope = {"Type": "Notification", "MessageId": "sns-id", "Message": json.dumps(ses)}
    return {"Body": json.dumps(envelope), "ReceiptHandle": "rh"}


@pytest.mark.asyncio
async def test_click_event_is_recorded(db_session) -> None:
    email = f"click-{uuid.uuid4()}@cadencetests.io"
    now = datetime.now(timezone.utc)
    await db_session.execute(
        text("""
            INSERT INTO recipients
                (id, tenant, email, external_id, vertical,
                 first_seen_at, last_seen_at, consent_source, consent_at, attributes)
            VALUES (:id, :t, :email, :ext, 'auto', :now, :now, 'otto_form', :now, '{}')
        """),
        {"id": str(uuid.uuid4()), "t": _TENANT, "email": email, "ext": f"ext-{uuid.uuid4()}", "now": now},
    )
    await db_session.commit()
    msg_id = f"ses-{uuid.uuid4()}"

    with patch("app.workers.sqs_event_poller._verify_envelope", return_value=True):
        await _process_message(db_session, _click_message(email, msg_id, now))

    # click_id is the join key origin_platform and profile_refresh use to reclassify.
    click_ids = (await db_session.execute(
        text("SELECT click_id FROM contact_event WHERE provider_msg_id = :m AND event_type = :c"),
        {"m": msg_id, "c": _CLICK},
    )).scalars().all()
    registry = (await db_session.execute(
        text("SELECT count(*) FROM click_registry WHERE click_id = 'ck-123' AND tenant = :t"),
        {"t": _TENANT},
    )).scalar_one()
    assert (click_ids, registry) == (["ck-123"], 1)
