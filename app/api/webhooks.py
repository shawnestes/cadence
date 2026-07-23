from __future__ import annotations

import asyncio
import json

import httpx
import structlog
from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.esp import ses as ses_mod
from app.adapters.esp.base import ParsedEvent
from app.adapters.esp.factory import get_esp_adapter
from app.core.db import get_db
from app.models.base import uuid7
from app.models.event import Event
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.sending_identity import SendingIdentity
from app.models.suppression import Suppression
from app.models.unsub_outbox import UnsubOutbox

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["webhooks"])

DEFAULT_TENANT = "otto"


async def _savepoint_add(db: AsyncSession, obj) -> None:
    """Add an idempotent row; swallow IntegrityError within a savepoint."""
    try:
        async with db.begin_nested():
            db.add(obj)
    except IntegrityError:
        logger.debug("webhook.duplicate_row_ignored", model=type(obj).__name__)


async def _auto_confirm_subscription(payload: dict) -> None:
    """Verify and auto-confirm an SNS SubscriptionConfirmation (SPEC §8.3).

    Signature verification (which may fetch/cache a signing cert over HTTPS)
    runs off the event loop via ``asyncio.to_thread``. Failures are logged,
    never raised, so a bad/forged confirmation just results in a no-op 200.
    """
    topic_arn = payload.get("TopicArn")
    subscribe_url = payload.get("SubscribeURL")

    verified = await asyncio.to_thread(ses_mod._verify_sns_signature, payload)
    if not verified:
        logger.warning(
            "webhook.subscription_confirmation_signature_invalid",
            topic_arn=topic_arn,
        )
        return

    if not subscribe_url or not ses_mod._is_allowed_cert_host(subscribe_url):
        logger.warning(
            "webhook.subscription_confirmation_url_rejected",
            topic_arn=topic_arn,
            subscribe_url=subscribe_url,
        )
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            resp = await http_client.get(subscribe_url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "webhook.subscription_confirmation_get_failed",
            topic_arn=topic_arn,
            error=str(exc),
        )
        return

    logger.info("webhook.subscription_confirmed", topic_arn=topic_arn)


def _extract_configuration_set(raw: dict) -> str | None:
    """Best-effort SES configuration-set name from a parsed event's raw payload.

    Real SES event JSON nests it at ``mail.tags["ses:configuration-set"][0]``.
    Some payloads (older events, synthetic test fixtures) carry it flat as
    ``configurationSetName`` instead. Neither shape present just means this
    event predates identity tagging, or came from an adapter that doesn't tag
    at all (e.g. NoopESP) — callers must treat that as "nothing to attribute",
    not an error.
    """
    mail = raw.get("mail")
    if isinstance(mail, dict):
        tags = mail.get("tags")
        if isinstance(tags, dict):
            values = tags.get("ses:configuration-set")
            if isinstance(values, list) and values and isinstance(values[0], str):
                return values[0]

    flat = raw.get("configurationSetName")
    if isinstance(flat, str) and flat:
        return flat

    return None


async def _resolve_configured_identity(
    db: AsyncSession, configuration_set: str
) -> SendingIdentity | None:
    """Look up the sending identity whose provider_config advertises this
    configuration set, for attribution telemetry only (SPEC_ADDENDUM_003 §6).
    """
    result = await db.execute(
        select(SendingIdentity).where(
            SendingIdentity.provider_config["configuration_set"].astext
            == configuration_set
        )
    )
    return result.scalars().first()


async def _check_identity_attribution(
    db: AsyncSession, evt: ParsedEvent, send: Send | None
) -> None:
    """Cross-check the webhook payload's configuration set against the send
    row's recorded sending identity. Telemetry/logging only — nothing is
    stored, and the send row is always trusted over the payload on mismatch.
    """
    configuration_set = _extract_configuration_set(evt.raw)
    if not configuration_set:
        return  # older event or an adapter that doesn't tag; skip silently

    if send is None or send.sending_identity_id is None:
        return  # nothing on our side to cross-check against

    configured_identity = await _resolve_configured_identity(db, configuration_set)
    if configured_identity is None:
        return  # config set doesn't map to any known identity

    if configured_identity.id != send.sending_identity_id:
        logger.warning(
            "webhook.identity_attribution_mismatch",
            esp_message_id=evt.esp_message_id,
            configuration_set=configuration_set,
            payload_identity_id=str(configured_identity.id),
            send_identity_id=str(send.sending_identity_id),
        )


async def _ingest_parsed_events(
    db: AsyncSession, events: list[ParsedEvent], esp_name: str
) -> None:
    """Write a provider's parsed webhook events to the DB.

    Handles the events insert, send status update, suppression on
    bounce/complaint, and unsub outbox — provider-agnostic so a future
    ``/webhooks/sendgrid`` route can reuse it without duplicating this logic.
    Commits once at the end, mirroring the prior single-commit behavior of
    ``ses_webhook``.
    """
    for evt in events:
        send: Send | None = None
        if evt.esp_message_id:
            result = await db.execute(
                select(Send).where(Send.esp_message_id == evt.esp_message_id)
            )
            send = result.scalar_one_or_none()

        await _check_identity_attribution(db, evt, send)

        tenant = send.tenant if send else DEFAULT_TENANT
        recipient: Recipient | None = None
        recipient_id = send.recipient_id if send else None

        if recipient_id is None and evt.recipient_email:
            rres = await db.execute(
                select(Recipient)
                .where(Recipient.tenant == DEFAULT_TENANT)
                .where(Recipient.email == evt.recipient_email)
            )
            recipient = rres.scalar_one_or_none()
            if recipient is not None:
                recipient_id = recipient.id
                tenant = recipient.tenant

        if recipient_id is None:
            logger.warning(
                "webhook.event_skipped_no_recipient",
                esp_message_id=evt.esp_message_id,
                event_type=evt.event_type,
                recipient_email=evt.recipient_email,
            )
            continue

        event_row = Event(
            id=uuid7(),
            tenant=tenant,
            send_id=send.id if send else None,
            recipient_id=recipient_id,
            campaign_id=send.campaign_id if send else None,
            variant_label=send.variant_label if send else None,
            cohort_key=send.cohort_key if send else None,
            event_type=evt.event_type,
            event_subtype=evt.event_subtype,
            url=evt.url,
            user_agent=evt.user_agent,
            raw_payload=evt.raw,
            occurred_at=evt.occurred_at,
        )
        db.add(event_row)

        if send is not None:
            if evt.event_type == "delivery":
                send.status = "delivered"
                send.delivered_at = evt.occurred_at
            elif evt.event_type == "bounce":
                if evt.event_subtype == "hard":
                    send.status = "bounced"
                    send.failure_reason = "hard_bounce"
                else:
                    send.status = "bounced"
                    send.failure_reason = "soft_bounce"
            elif evt.event_type == "complaint":
                send.status = "complained"
            elif evt.event_type == "reject":
                send.status = "failed"
                send.failure_reason = "rejected"

        email_for_suppress: str | None = None
        if recipient is None and send is not None:
            rres = await db.execute(select(Recipient).where(Recipient.id == send.recipient_id))
            recipient = rres.scalar_one_or_none()
        if recipient is not None:
            email_for_suppress = recipient.email
        elif evt.recipient_email:
            email_for_suppress = evt.recipient_email

        suppress_reason: str | None = None
        new_recipient_status: str | None = None
        if evt.event_type == "bounce" and evt.event_subtype == "hard":
            suppress_reason = "hard_bounce"
            new_recipient_status = "bounced_hard"
        elif evt.event_type == "complaint":
            suppress_reason = "complaint"
            new_recipient_status = "complained"

        if suppress_reason and email_for_suppress:
            await _savepoint_add(
                db,
                Suppression(
                    id=uuid7(),
                    tenant=tenant,
                    email=email_for_suppress,
                    reason=suppress_reason,
                    source="sns",
                ),
            )
            await _savepoint_add(
                db,
                UnsubOutbox(
                    id=uuid7(),
                    tenant=tenant,
                    email=email_for_suppress,
                    reason=suppress_reason,
                    occurred_at=evt.occurred_at,
                ),
            )
            if recipient is not None and new_recipient_status:
                recipient.status = new_recipient_status

    await db.commit()


@router.post("/webhooks/ses", status_code=status.HTTP_200_OK)
async def ses_webhook(
    request: Request,
    x_amz_sns_message_type: str | None = Header(None, alias="x-amz-sns-message-type"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    body = await request.body()
    try:
        payload = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("webhook.malformed_body", error=str(exc))
        return Response(status_code=200)

    msg_type = payload.get("Type") or x_amz_sns_message_type

    if msg_type == "SubscriptionConfirmation":
        logger.info(
            "webhook.subscription_confirmation_received",
            topic_arn=payload.get("TopicArn"),
            subscribe_url=payload.get("SubscribeURL"),
        )
        await _auto_confirm_subscription(payload)
        return Response(status_code=200)

    if msg_type == "UnsubscribeConfirmation":
        logger.info("webhook.unsubscribe_confirmation", topic_arn=payload.get("TopicArn"))
        return Response(status_code=200)

    if msg_type and msg_type != "Notification":
        logger.info("webhook.unknown_sns_type", type=msg_type)
        return Response(status_code=200)

    try:
        adapter = get_esp_adapter()
        # parse_webhook may synchronously fetch/cache an SNS signing cert over
        # HTTPS on a cache miss; run it off the event loop so a slow/hanging
        # fetch can't block other requests for up to 5s.
        events = await asyncio.to_thread(
            adapter.parse_webhook, payload, payload.get("Signature")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook.parse_failed", error=str(exc))
        return Response(status_code=200)

    await _ingest_parsed_events(db, events, adapter.name)
    return Response(status_code=200)
