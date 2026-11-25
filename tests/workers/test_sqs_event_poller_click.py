"""SES Click ingestion against a real DB.

The mocked poller tests cannot catch SQL that Postgres rejects; every Click
event once failed this way and stayed in the queue unrecorded.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
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


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _ses_message(event_type: str, detail: dict, email: str, msg_id: str, sent_at: datetime) -> dict:
    ses = {
        "eventType": event_type,
        "mail": {
            "messageId": msg_id,
            "timestamp": _iso(sent_at),
            "destination": [email],
            "tags": {"tenant": [_TENANT]},
        },
        event_type.lower(): detail,
    }
    envelope = {"Type": "Notification", "MessageId": "sns-id", "Message": json.dumps(ses)}
    return {"Body": json.dumps(envelope), "ReceiptHandle": "rh"}


@pytest.mark.asyncio
async def test_click_event_is_recorded_at_click_time(db_session) -> None:
    email = f"click-{uuid.uuid4()}@cadencetests.io"
    sent_at = datetime.now(timezone.utc).replace(microsecond=0)
    clicked_at = sent_at + timedelta(minutes=5)
    await db_session.execute(
        text("""
            INSERT INTO recipients
                (id, tenant, email, external_id, vertical,
                 first_seen_at, last_seen_at, consent_source, consent_at, attributes)
            VALUES (:id, :t, :email, :ext, 'auto', :now, :now, 'otto_form', :now, '{}')
        """),
        {"id": str(uuid.uuid4()), "t": _TENANT, "email": email, "ext": f"ext-{uuid.uuid4()}", "now": sent_at},
    )
    await db_session.commit()
    msg_id = f"ses-{uuid.uuid4()}"

    delivery = {"timestamp": _iso(sent_at + timedelta(seconds=3))}
    click = {
        "timestamp": _iso(clicked_at),
        "linkUrl": "https://example.com/quote?click_id=ck-123",
        "ipAddress": "203.0.113.9",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    with patch("app.workers.sqs_event_poller._verify_envelope", return_value=True):
        await _process_message(db_session, _ses_message("Delivery", delivery, email, msg_id, sent_at))
        await _process_message(db_session, _ses_message("Click", click, email, msg_id, sent_at))

    # click_id is the join key origin_platform and profile_refresh use to reclassify.
    # Stamping the send time instead of the click time put every click before
    # its delivery, so the <2s-after-delivery rule marked all of them scanner.
    rows = (await db_session.execute(
        text("SELECT click_id, occurred_at, classification FROM contact_event"
             " WHERE provider_msg_id = :m AND event_type = :c"),
        {"m": msg_id, "c": _CLICK},
    )).all()
    registry = (await db_session.execute(
        text("SELECT count(*) FROM click_registry WHERE click_id = 'ck-123' AND tenant = :t"),
        {"t": _TENANT},
    )).scalar_one()
    assert [tuple(r) for r in rows] == [("ck-123", clicked_at, 0)]
    assert registry == 1
