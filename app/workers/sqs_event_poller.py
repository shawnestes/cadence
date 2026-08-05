"""SQS event poller — drains the SES→SNS→SQS queue and writes to the event store.

When sqs_event_queue_url is None (or AWS creds are absent) this is a no-op so
the scheduler can register the job unconditionally.

D5: parallel-write; existing events table is untouched.
D6: AWS infra may not yet exist; the worker degrades gracefully.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import boto3
import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.esp.ses import _verify_sns_signature
from app.core.config import get_settings
from app.core.db import async_session_factory
from app.domain.click_classification import classify_click
from app.domain.event_codes import translate_ses_event_type
from app.domain.open_classification import classify_open
from app.models.base import uuid7
from app.models.recipient import Recipient

log = structlog.get_logger(__name__)

# Source constant: 1 = SES (D5)
_SOURCE_SES: int = 1
# Channel constant: 1 = email
_CHANNEL_EMAIL: int = 1


def _build_sqs_client() -> Any:
    settings = get_settings()
    return boto3.client("sqs", region_name=settings.aws_region)


def _parse_sns_envelope(raw_body: str) -> dict[str, Any]:
    """Parse and optionally verify the SNS envelope from the raw SQS message body."""
    envelope: dict[str, Any] = json.loads(raw_body)
    return envelope


def _verify_envelope(envelope: dict[str, Any]) -> bool:
    """Verify SNS signature. Returns True when valid or verification is disabled."""
    settings = get_settings()
    if not settings.sns_verify_signatures:
        return True
    return _verify_sns_signature(envelope)


def _parse_ses_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    """Extract the SES event JSON from the SNS Message field."""
    return json.loads(envelope["Message"])


def _resolve_tenant(ses_payload: dict[str, Any]) -> str:
    """Derive tenant from SES ConfigurationSet tag or fall back to alert_tenants[0]."""
    settings = get_settings()
    # SES sends a 'configurationSet' field; cross-reference against settings or tags.
    # For now, fall back to the primary tenant — multi-tenant routing is a later milestone.
    tags: list[dict[str, str]] = (
        ses_payload.get("mail", {}).get("tags", {}) or {}
    )
    if isinstance(tags, dict):
        tenant_tag = tags.get("tenant")
        if isinstance(tenant_tag, list) and tenant_tag:
            return tenant_tag[0]
        if isinstance(tenant_tag, str) and tenant_tag:
            return tenant_tag
    elif isinstance(tags, list):
        for tag in tags:
            if tag.get("name") == "tenant":
                return tag.get("value", settings.alert_tenants[0])
    return settings.alert_tenants[0]


async def _resolve_contact_id(
    db: AsyncSession, tenant: str, email: str
) -> uuid.UUID | None:
    """Look up the recipient id by tenant + email. Returns None if not found."""
    row = await db.execute(
        select(Recipient.id).where(
            Recipient.tenant == tenant,
            Recipient.email == email,
        )
    )
    result = row.scalar_one_or_none()
    return result


async def _insert_contact_event(
    db: AsyncSession,
    tenant: str,
    contact_id: uuid.UUID,
    event_type_code: int,
    occurred_at: datetime,
    provider_msg_id: str,
    meta: dict[str, Any] | None,
    classification: int | None = None,
) -> None:
    """Insert a contact_event row with ON CONFLICT DO NOTHING."""
    await db.execute(
        text(
            """
            INSERT INTO contact_event
                (event_id, occurred_at, tenant, contact_id, channel, event_type,
                 source, provider_msg_id, classification, meta)
            VALUES
                (:event_id, :occurred_at, :tenant, :contact_id, :channel,
                 :event_type, :source, :provider_msg_id, :classification, :meta)
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "event_id": str(uuid7()),
            "occurred_at": occurred_at,
            "tenant": tenant,
            "contact_id": str(contact_id),
            "channel": _CHANNEL_EMAIL,
            "event_type": event_type_code,
            "source": _SOURCE_SES,
            "provider_msg_id": provider_msg_id,
            "classification": classification,
            "meta": json.dumps(meta) if meta else None,
        },
    )


async def _insert_suppression(
    db: AsyncSession, tenant: str, email: str, reason: str
) -> None:
    """Insert a suppression row with ON CONFLICT DO NOTHING."""
    await db.execute(
        text(
            """
            INSERT INTO suppressions (id, tenant, email, reason, source)
            VALUES (:id, :tenant, :email, :reason, :source)
            ON CONFLICT ON CONSTRAINT uq_suppressions_tenant_email DO NOTHING
            """
        ),
        {
            "id": str(uuid7()),
            "tenant": tenant,
            "email": email,
            "reason": reason,
            "source": "ses",
        },
    )


async def _process_message(
    db: AsyncSession, sqs_message: dict[str, Any]
) -> None:
    """Parse and ingest one SQS message into contact_event (and suppression if needed)."""
    body: str = sqs_message["Body"]

    # 1. Parse SNS envelope.
    try:
        envelope = _parse_sns_envelope(body)
    except (json.JSONDecodeError, KeyError) as exc:
        log.warning("sqs_poller.sns_parse_failed", error=str(exc))
        raise

    # 2. Verify SNS signature.
    if not _verify_envelope(envelope):
        raise ValueError("SNS signature verification failed")

    # 3. Parse SES payload from SNS Message field.
    try:
        ses_payload = _parse_ses_payload(envelope)
    except (json.JSONDecodeError, KeyError) as exc:
        log.warning("sqs_poller.ses_payload_parse_failed", error=str(exc))
        raise

    event_type_str: str = ses_payload.get("eventType", "")
    mail: dict[str, Any] = ses_payload.get("mail", {})
    msg_id: str = mail.get("messageId", "")
    timestamp_str: str = mail.get("timestamp", "")
    destinations: list[str] = mail.get("destination", [])

    try:
        occurred_at = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        occurred_at = datetime.now(tz=timezone.utc)

    # 4. Resolve tenant.
    tenant = _resolve_tenant(ses_payload)

    # 5. Resolve contact_id.
    email = destinations[0] if destinations else ""
    if not email:
        log.warning("sqs_poller.no_destination_email", msg_id=msg_id)
        raise ValueError("No destination email in SES payload")

    contact_id = await _resolve_contact_id(db, tenant, email)
    if contact_id is None:
        log.info("sqs_poller.contact_not_found", email=email, tenant=tenant)
        # Signal caller to delete message (unknown contact — not an error).
        return

    # 6. Translate event type string → smallint.
    try:
        event_type_code = translate_ses_event_type(event_type_str)
    except ValueError:
        log.warning(
            "sqs_poller.unknown_event_type",
            event_type=event_type_str,
            msg_id=msg_id,
        )
        raise

    settings = get_settings()

    # Build meta from event-type-specific fields.
    meta: dict[str, Any] | None = None
    classification: int | None = None
    click_id: str | None = None
    link_url: str | None = None
    click_ip: str | None = None
    click_ua: str | None = None

    if event_type_str == "Bounce":
        bounce = ses_payload.get("bounce", {})
        meta = {
            "bounceType": bounce.get("bounceType"),
            "bounceSubType": bounce.get("bounceSubType"),
        }
    elif event_type_str == "Click":
        click = ses_payload.get("click", {})
        link_url = click.get("linkUrl")
        click_ip = click.get("ipAddress")
        click_ua = click.get("userAgent")
        meta = {"ipAddress": click_ip, "userAgent": click_ua}

        # Extract click_id from link URL query params.
        if link_url:
            parsed = urlparse(link_url)
            qs = parse_qs(parsed.query)
            click_id_list = qs.get("click_id")
            if click_id_list:
                click_id = click_id_list[0]

        # Fetch delivered_at: most recent Delivery event for this message.
        delivery_code = 2  # SES_EVENT_TYPE_CODES["Delivery"]
        delivery_row = await db.execute(
            text(
                """
                SELECT occurred_at FROM contact_event
                WHERE provider_msg_id = :msg_id
                  AND event_type = :delivery_code
                  AND tenant = :tenant
                ORDER BY occurred_at DESC
                LIMIT 1
                """
            ),
            {"msg_id": msg_id, "delivery_code": delivery_code, "tenant": tenant},
        )
        delivered_at: datetime | None = delivery_row.scalar_one_or_none()

        # Count other links clicked within the same second (rule 2).
        click_code = 7  # SES_EVENT_TYPE_CODES["Click"]
        same_second_row = await db.execute(
            text(
                """
                SELECT COUNT(*) FROM contact_event
                WHERE contact_id = :contact_id
                  AND provider_msg_id = :msg_id
                  AND event_type = :click_code
                  AND tenant = :tenant
                  AND date_trunc('second', occurred_at) = date_trunc('second', :occurred_at::timestamptz)
                """
            ),
            {
                "contact_id": str(contact_id),
                "msg_id": msg_id,
                "click_code": click_code,
                "tenant": tenant,
                "occurred_at": occurred_at,
            },
        )
        other_links_same_second: int = (same_second_row.scalar_one() or 0)

        classification = classify_click(
            user_agent=click_ua,
            ip=click_ip,
            clicked_at=occurred_at,
            delivered_at=delivered_at,
            other_links_same_second=other_links_same_second,
            has_origin_platform_confirm=False,
            confirm_window_s=settings.scanner_confirm_window_s,
            known_scanner_uas=settings.known_scanner_uas,
            apple_proxy_cidrs=settings.apple_proxy_ip_ranges,
        )

    elif event_type_str == "Open":
        open_data = ses_payload.get("open", {})
        open_ip = open_data.get("ipAddress")
        open_ua = open_data.get("userAgent")
        meta = {"ipAddress": open_ip, "userAgent": open_ua}
        classification = classify_open(
            ip=open_ip,
            user_agent=open_ua,
            apple_proxy_cidrs=settings.apple_proxy_ip_ranges,
        )

    elif event_type_str == "Complaint":
        complaint = ses_payload.get("complaint", {})
        meta = {"feedbackType": complaint.get("complaintFeedbackType")}

    # 7. Insert into contact_event with ON CONFLICT DO NOTHING.
    await _insert_contact_event(
        db,
        tenant=tenant,
        contact_id=contact_id,
        event_type_code=event_type_code,
        occurred_at=occurred_at,
        provider_msg_id=msg_id,
        meta=meta,
        classification=classification,
    )

    # 7b. For Click events with a click_id, upsert click_registry.
    if event_type_str == "Click" and click_id is not None:
        await db.execute(
            text(
                """
                INSERT INTO click_registry
                    (click_id, tenant, contact_id, channel, campaign_id, clicked_at, classification)
                VALUES
                    (:click_id, :tenant, :contact_id, :channel, NULL, :clicked_at, :classification)
                ON CONFLICT (click_id) DO NOTHING
                """
            ),
            {
                "click_id": click_id,
                "tenant": tenant,
                "contact_id": str(contact_id),
                "channel": _CHANNEL_EMAIL,
                "clicked_at": occurred_at,
                "classification": classification if classification is not None else 0,
            },
        )

    # SPEC §5 step 4: upsert contact_activity_daily counter for this event.
    # Determine which column to increment based on event type and classification.
    _ACTIVITY_COLUMN: str | None = None
    if event_type_str == "Send":
        _ACTIVITY_COLUMN = "sends"
    elif event_type_str == "Delivery":
        _ACTIVITY_COLUMN = "delivers"
    elif event_type_str == "Open":
        # classification=1 is MPP; classification=0 is human open
        _ACTIVITY_COLUMN = "opens_mpp" if classification == 1 else "opens"
    elif event_type_str == "Click":
        # classification=2 is scanner; 0 is human
        _ACTIVITY_COLUMN = "clicks_bot" if classification == 2 else "clicks"
    elif event_type_str == "Bounce":
        _ACTIVITY_COLUMN = "bounces"
    elif event_type_str == "Complaint":
        _ACTIVITY_COLUMN = "complaints"

    if _ACTIVITY_COLUMN is not None:
        await db.execute(
            text(
                f"""
                INSERT INTO contact_activity_daily
                    (contact_id, tenant, channel, day, {_ACTIVITY_COLUMN})
                VALUES
                    (:contact_id, :tenant, :channel, :day, 1)
                ON CONFLICT (tenant, contact_id, channel, day) DO UPDATE
                    SET {_ACTIVITY_COLUMN} = contact_activity_daily.{_ACTIVITY_COLUMN} + 1
                """
            ),
            {
                "contact_id": str(contact_id),
                "tenant": tenant,
                "channel": _CHANNEL_EMAIL,
                "day": occurred_at.date(),
            },
        )

    # SPEC §5 step 5: bump profile_version to mark contact dirty for the incremental pass.
    await db.execute(
        text(
            """
            UPDATE contact_profile
               SET profile_version = profile_version + 1
             WHERE contact_id = :contact_id
               AND tenant     = :tenant
               AND channel    = :channel
            """
        ),
        {
            "contact_id": str(contact_id),
            "tenant": tenant,
            "channel": _CHANNEL_EMAIL,
        },
    )

    # 8. Suppress on hard bounce or complaint.
    if event_type_str == "Bounce":
        bounce_type = ses_payload.get("bounce", {}).get("bounceType", "")
        if bounce_type == "Permanent":
            await _insert_suppression(db, tenant, email, reason="hard_bounce")
    elif event_type_str == "Complaint":
        await _insert_suppression(db, tenant, email, reason="complaint")

    await db.commit()


async def _delete_sqs_message(
    sqs_client: Any, queue_url: str, receipt_handle: str
) -> None:
    """Delete a processed SQS message."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: sqs_client.delete_message(
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle,
        ),
    )


async def _poll_once(sqs_client: Any, queue_url: str) -> None:
    """Receive up to 10 messages and process each one."""
    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        None,
        lambda: sqs_client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=1,
        ),
    )
    messages: list[dict[str, Any]] = response.get("Messages", [])

    for msg in messages:
        receipt_handle: str = msg["ReceiptHandle"]
        try:
            async with async_session_factory() as db:
                await _process_message(db, msg)
            await _delete_sqs_message(sqs_client, queue_url, receipt_handle)
        except Exception as exc:
            log.warning(
                "sqs_poller.message_processing_failed",
                receipt_handle=receipt_handle,
                error=str(exc),
            )
            # Do NOT delete — message will reappear after visibility timeout / go to DLQ.


async def run_once() -> None:
    """APScheduler entry point. Returns immediately when sqs_event_queue_url is None."""
    settings = get_settings()
    if not settings.sqs_event_queue_url:
        return

    try:
        sqs_client = _build_sqs_client()
        await _poll_once(sqs_client, settings.sqs_event_queue_url)
    except Exception as exc:
        log.error("sqs_poller.poll_cycle_failed", error=str(exc))
