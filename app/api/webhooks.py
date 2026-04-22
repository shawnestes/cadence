from __future__ import annotations

import json

import httpx
import structlog
from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.esp.factory import get_esp_adapter
from app.core.db import get_db
from app.models.base import uuid7
from app.models.event import Event
from app.models.recipient import Recipient
from app.models.send import Send
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
        subscribe_url = payload.get("SubscribeURL")
        if subscribe_url:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(subscribe_url)
                logger.info(
                    "webhook.subscription_confirmed",
                    status=resp.status_code,
                    topic_arn=payload.get("TopicArn"),
                )
            except httpx.HTTPError as exc:
                logger.warning("webhook.subscription_confirm_failed", error=str(exc))
        return Response(status_code=200)

    if msg_type == "UnsubscribeConfirmation":
        logger.info("webhook.unsubscribe_confirmation", topic_arn=payload.get("TopicArn"))
        return Response(status_code=200)

    if msg_type and msg_type != "Notification":
        logger.info("webhook.unknown_sns_type", type=msg_type)
        return Response(status_code=200)

    try:
        adapter = get_esp_adapter()
        events = adapter.parse_webhook(payload, payload.get("Signature"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook.parse_failed", error=str(exc))
        return Response(status_code=200)

    for evt in events:
        send: Send | None = None
        if evt.esp_message_id:
            result = await db.execute(
                select(Send).where(Send.esp_message_id == evt.esp_message_id)
            )
            send = result.scalar_one_or_none()

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
    return Response(status_code=200)
