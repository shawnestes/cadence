"""Unit tests for app.workers.sqs_event_poller.

All tests use unittest.mock — no live DB or AWS required.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TENANT = "otto"
_EMAIL = "test-poller@cadencetests.io"
_MSG_ID = "ses-message-id-abc123"
_RECEIPT = "receipt-handle-xyz"
_OCCURRED_AT = "2024-01-15T12:00:00Z"
_CONTACT_ID = uuid.uuid4()


def _sns_envelope(ses_payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap an SES payload in a minimal SNS envelope."""
    return {
        "Type": "Notification",
        "MessageId": "sns-envelope-id",
        "Message": json.dumps(ses_payload),
    }


def _ses_payload(
    event_type: str,
    email: str = _EMAIL,
    msg_id: str = _MSG_ID,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventType": event_type,
        "mail": {
            "messageId": msg_id,
            "timestamp": _OCCURRED_AT,
            "destination": [email],
        },
    }
    if extra:
        payload.update(extra)
    return payload


def _sqs_message(
    ses_payload: dict[str, Any],
    receipt: str = _RECEIPT,
) -> dict[str, Any]:
    return {
        "Body": json.dumps(_sns_envelope(ses_payload)),
        "ReceiptHandle": receipt,
    }


# ---------------------------------------------------------------------------
# Test 1 — no-op when queue URL is unset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_once_noop_when_queue_url_none() -> None:
    """run_once returns immediately without calling boto3 when queue URL is None."""
    with (
        patch("app.workers.sqs_event_poller.get_settings") as mock_settings,
        patch("app.workers.sqs_event_poller.boto3") as mock_boto3,
    ):
        settings = MagicMock()
        settings.sqs_event_queue_url = None
        mock_settings.return_value = settings

        from app.workers.sqs_event_poller import run_once

        await run_once()

        mock_boto3.client.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2 — dedup: both messages deleted when ON CONFLICT fires on second
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dedup_both_messages_deleted() -> None:
    """Two identical SES Open events: first insert succeeds, second hits ON CONFLICT.

    Both SQS messages must be deleted — ON CONFLICT DO NOTHING is not an error.
    """
    payload = _ses_payload("Open")
    msg1 = _sqs_message(payload, receipt="receipt-1")
    msg2 = _sqs_message(payload, receipt="receipt-2")

    mock_sqs = MagicMock()
    mock_sqs.receive_message.return_value = {"Messages": [msg1, msg2]}
    mock_sqs.delete_message = MagicMock()

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=MagicMock())
    mock_db.commit = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    # Scalar for contact lookup: return a UUID (contact found)
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = _CONTACT_ID
    mock_db.execute.return_value = scalar_result

    with (
        patch("app.workers.sqs_event_poller.get_settings") as mock_settings,
        patch("app.workers.sqs_event_poller._build_sqs_client", return_value=mock_sqs),
        patch("app.workers.sqs_event_poller.async_session_factory") as mock_factory,
        patch("app.workers.sqs_event_poller._verify_envelope", return_value=True),
    ):
        settings = MagicMock()
        settings.sqs_event_queue_url = "https://sqs.us-east-1.amazonaws.com/123/test"
        settings.alert_tenants = [_TENANT]
        settings.sns_verify_signatures = False
        mock_settings.return_value = settings

        mock_factory.return_value = mock_db

        from app.workers.sqs_event_poller import _poll_once

        await _poll_once(mock_sqs, settings.sqs_event_queue_url)

    # Both receipts must be deleted
    deleted_receipts = [
        c.kwargs.get("ReceiptHandle") or c.args[0] if c.args else c.kwargs["ReceiptHandle"]
        for c in mock_sqs.delete_message.call_args_list
    ]
    # Extract ReceiptHandle from kwargs
    deleted_receipts = [
        c.kwargs["ReceiptHandle"] for c in mock_sqs.delete_message.call_args_list
    ]
    assert "receipt-1" in deleted_receipts
    assert "receipt-2" in deleted_receipts
    assert mock_sqs.delete_message.call_count == 2


# ---------------------------------------------------------------------------
# Test 3 — MPP open classification: Apple UA gets classification=1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mpp_open_classification_apple_ua() -> None:
    """Open event from an Apple Mail UA inserts contact_event with classification=1 (MPP)."""
    payload = _ses_payload("Open", extra={"open": {"ipAddress": "1.2.3.4", "userAgent": "AppleMail"}})
    msg = _sqs_message(payload)

    captured_sql_params: list[dict[str, Any]] = []

    async def capture_execute(stmt: Any, params: Any = None) -> MagicMock:
        if params and isinstance(params, dict):
            captured_sql_params.append(dict(params))
        result = MagicMock()
        result.scalar_one_or_none.return_value = _CONTACT_ID
        return result

    mock_db = AsyncMock()
    mock_db.execute = capture_execute
    mock_db.commit = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    mock_sqs = MagicMock()
    mock_sqs.receive_message.return_value = {"Messages": [msg]}
    mock_sqs.delete_message = MagicMock()

    with (
        patch("app.workers.sqs_event_poller.get_settings") as mock_settings,
        patch("app.workers.sqs_event_poller._build_sqs_client", return_value=mock_sqs),
        patch("app.workers.sqs_event_poller.async_session_factory") as mock_factory,
        patch("app.workers.sqs_event_poller._verify_envelope", return_value=True),
    ):
        settings = MagicMock()
        settings.sqs_event_queue_url = "https://sqs.us-east-1.amazonaws.com/123/test"
        settings.alert_tenants = [_TENANT]
        settings.sns_verify_signatures = False
        settings.apple_proxy_ip_ranges = ["17.0.0.0/8"]
        settings.known_scanner_uas = []
        mock_settings.return_value = settings

        mock_factory.return_value = mock_db

        from app.workers.sqs_event_poller import _poll_once

        await _poll_once(mock_sqs, settings.sqs_event_queue_url)

    # Find the contact_event insert (has 'event_id' key)
    insert_params = [p for p in captured_sql_params if "event_id" in p]
    assert insert_params, "Expected at least one contact_event INSERT"

    for p in insert_params:
        # Apple Mail UA ("AppleMail" contains "apple" and "mail") → CLASS_MPP = 1
        assert p.get("classification") == 1, (
            f"Expected classification=1 (MPP) for Apple UA, got {p.get('classification')}"
        )


# ---------------------------------------------------------------------------
# Test 4 — Complaint triggers suppression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complaint_triggers_suppression() -> None:
    """SNS/SES Complaint event causes a Suppression INSERT and message deletion."""
    payload = _ses_payload("Complaint", extra={"complaint": {"complaintFeedbackType": "abuse"}})
    msg = _sqs_message(payload)

    sql_texts: list[str] = []
    sql_params: list[dict[str, Any]] = []

    async def capture_execute(stmt: Any, params: Any = None) -> MagicMock:
        stmt_str = str(stmt) if not isinstance(stmt, str) else stmt
        sql_texts.append(stmt_str)
        if params and isinstance(params, dict):
            sql_params.append(dict(params))
        result = MagicMock()
        result.scalar_one_or_none.return_value = _CONTACT_ID
        return result

    mock_db = AsyncMock()
    mock_db.execute = capture_execute
    mock_db.commit = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    mock_sqs = MagicMock()
    mock_sqs.receive_message.return_value = {"Messages": [msg]}
    mock_sqs.delete_message = MagicMock()

    with (
        patch("app.workers.sqs_event_poller.get_settings") as mock_settings,
        patch("app.workers.sqs_event_poller._build_sqs_client", return_value=mock_sqs),
        patch("app.workers.sqs_event_poller.async_session_factory") as mock_factory,
        patch("app.workers.sqs_event_poller._verify_envelope", return_value=True),
        patch("app.workers.sqs_event_poller._insert_suppression", new_callable=AsyncMock) as mock_suppress,
    ):
        settings = MagicMock()
        settings.sqs_event_queue_url = "https://sqs.us-east-1.amazonaws.com/123/test"
        settings.alert_tenants = [_TENANT]
        settings.sns_verify_signatures = False
        mock_settings.return_value = settings

        mock_factory.return_value = mock_db

        from app.workers.sqs_event_poller import _poll_once

        await _poll_once(mock_sqs, settings.sqs_event_queue_url)

    mock_suppress.assert_called_once()
    _tenant_arg, _email_arg, reason_kwarg = (
        mock_suppress.call_args.args[1],
        mock_suppress.call_args.args[2],
        mock_suppress.call_args.args[3] if len(mock_suppress.call_args.args) > 3
        else mock_suppress.call_args.kwargs.get("reason"),
    )
    # Accept positional or keyword
    call_args = mock_suppress.call_args
    reason = call_args.args[3] if len(call_args.args) > 3 else call_args.kwargs.get("reason")
    assert reason == "complaint"
    mock_sqs.delete_message.assert_called_once()


# ---------------------------------------------------------------------------
# Test 5 — Hard bounce triggers suppression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hard_bounce_triggers_suppression() -> None:
    """Permanent bounce causes a Suppression INSERT and message deletion."""
    payload = _ses_payload(
        "Bounce",
        extra={"bounce": {"bounceType": "Permanent", "bounceSubType": "General"}},
    )
    msg = _sqs_message(payload)

    mock_db = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = _CONTACT_ID
    mock_db.execute = AsyncMock(return_value=scalar_result)
    mock_db.commit = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    mock_sqs = MagicMock()
    mock_sqs.receive_message.return_value = {"Messages": [msg]}
    mock_sqs.delete_message = MagicMock()

    with (
        patch("app.workers.sqs_event_poller.get_settings") as mock_settings,
        patch("app.workers.sqs_event_poller._build_sqs_client", return_value=mock_sqs),
        patch("app.workers.sqs_event_poller.async_session_factory") as mock_factory,
        patch("app.workers.sqs_event_poller._verify_envelope", return_value=True),
        patch("app.workers.sqs_event_poller._insert_suppression", new_callable=AsyncMock) as mock_suppress,
    ):
        settings = MagicMock()
        settings.sqs_event_queue_url = "https://sqs.us-east-1.amazonaws.com/123/test"
        settings.alert_tenants = [_TENANT]
        settings.sns_verify_signatures = False
        mock_settings.return_value = settings

        mock_factory.return_value = mock_db

        from app.workers.sqs_event_poller import _poll_once

        await _poll_once(mock_sqs, settings.sqs_event_queue_url)

    mock_suppress.assert_called_once()
    call_args = mock_suppress.call_args
    reason = call_args.args[3] if len(call_args.args) > 3 else call_args.kwargs.get("reason")
    assert reason == "hard_bounce"
    mock_sqs.delete_message.assert_called_once()


# ---------------------------------------------------------------------------
# Test 6 — Soft bounce does NOT trigger suppression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_soft_bounce_no_suppression() -> None:
    """Transient bounce must NOT insert a suppression row."""
    payload = _ses_payload(
        "Bounce",
        extra={"bounce": {"bounceType": "Transient", "bounceSubType": "MailboxFull"}},
    )
    msg = _sqs_message(payload)

    mock_db = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = _CONTACT_ID
    mock_db.execute = AsyncMock(return_value=scalar_result)
    mock_db.commit = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    mock_sqs = MagicMock()
    mock_sqs.receive_message.return_value = {"Messages": [msg]}
    mock_sqs.delete_message = MagicMock()

    with (
        patch("app.workers.sqs_event_poller.get_settings") as mock_settings,
        patch("app.workers.sqs_event_poller._build_sqs_client", return_value=mock_sqs),
        patch("app.workers.sqs_event_poller.async_session_factory") as mock_factory,
        patch("app.workers.sqs_event_poller._verify_envelope", return_value=True),
        patch("app.workers.sqs_event_poller._insert_suppression", new_callable=AsyncMock) as mock_suppress,
    ):
        settings = MagicMock()
        settings.sqs_event_queue_url = "https://sqs.us-east-1.amazonaws.com/123/test"
        settings.alert_tenants = [_TENANT]
        settings.sns_verify_signatures = False
        mock_settings.return_value = settings

        mock_factory.return_value = mock_db

        from app.workers.sqs_event_poller import _poll_once

        await _poll_once(mock_sqs, settings.sqs_event_queue_url)

    mock_suppress.assert_not_called()
    # Message should still be deleted (successful processing)
    mock_sqs.delete_message.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7 — Unknown recipient: no insert, message IS deleted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_recipient_skips_insert_deletes_message() -> None:
    """When contact_id lookup returns None, no contact_event is inserted but the message is deleted."""
    payload = _ses_payload("Delivery")
    msg = _sqs_message(payload)

    sql_calls: list[Any] = []

    async def capture_execute(stmt: Any, params: Any = None) -> MagicMock:
        sql_calls.append((stmt, params))
        result = MagicMock()
        # Contact lookup returns None — unknown recipient
        result.scalar_one_or_none.return_value = None
        return result

    mock_db = AsyncMock()
    mock_db.execute = capture_execute
    mock_db.commit = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    mock_sqs = MagicMock()
    mock_sqs.receive_message.return_value = {"Messages": [msg]}
    mock_sqs.delete_message = MagicMock()

    with (
        patch("app.workers.sqs_event_poller.get_settings") as mock_settings,
        patch("app.workers.sqs_event_poller._build_sqs_client", return_value=mock_sqs),
        patch("app.workers.sqs_event_poller.async_session_factory") as mock_factory,
        patch("app.workers.sqs_event_poller._verify_envelope", return_value=True),
    ):
        settings = MagicMock()
        settings.sqs_event_queue_url = "https://sqs.us-east-1.amazonaws.com/123/test"
        settings.alert_tenants = [_TENANT]
        settings.sns_verify_signatures = False
        mock_settings.return_value = settings

        mock_factory.return_value = mock_db

        from app.workers.sqs_event_poller import _poll_once

        await _poll_once(mock_sqs, settings.sqs_event_queue_url)

    # No contact_event INSERT should have been executed (only the SELECT for contact lookup)
    insert_calls = [
        (stmt, params)
        for stmt, params in sql_calls
        if params and isinstance(params, dict) and "event_id" in params
    ]
    assert insert_calls == [], "contact_event must NOT be inserted for unknown recipients"

    # Message IS deleted because unknown-contact is not retryable
    mock_sqs.delete_message.assert_called_once()


# ---------------------------------------------------------------------------
# Test 8 — Exception per message does not crash run_once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exception_per_message_does_not_crash_run_once() -> None:
    """A bad message body that raises during parsing must not crash run_once().

    The message is NOT deleted (visibility timeout / DLQ handles it).
    """
    bad_msg = {
        "Body": "not-valid-json{{{",
        "ReceiptHandle": "bad-receipt",
    }

    mock_sqs = MagicMock()
    mock_sqs.receive_message.return_value = {"Messages": [bad_msg]}
    mock_sqs.delete_message = MagicMock()

    with (
        patch("app.workers.sqs_event_poller.get_settings") as mock_settings,
        patch("app.workers.sqs_event_poller._build_sqs_client", return_value=mock_sqs),
        patch("app.workers.sqs_event_poller.async_session_factory") as mock_factory,
        patch("app.workers.sqs_event_poller._verify_envelope", return_value=True),
    ):
        settings = MagicMock()
        settings.sqs_event_queue_url = "https://sqs.us-east-1.amazonaws.com/123/test"
        settings.alert_tenants = [_TENANT]
        settings.sns_verify_signatures = False
        mock_settings.return_value = settings

        # DB session shouldn't even be reached, but provide a mock in case
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_db

        from app.workers.sqs_event_poller import run_once

        # Must not raise
        await run_once()

    # Bad message must NOT be deleted
    mock_sqs.delete_message.assert_not_called()
