from __future__ import annotations

import asyncio
import functools
import json
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import boto3
import structlog
from botocore.exceptions import ClientError

from app.adapters.esp.base import (
    ESPAdapter,
    OutboundMessage,
    ParsedEvent,
    ReputationSnapshot,
    SendResult,
)

logger = structlog.get_logger(__name__)


_BOUNCE_SUBTYPE_MAP = {
    "Permanent": "hard",
    "Transient": "soft",
    "Undetermined": "soft",
}


class SESError(Exception):
    """Wraps a botocore ClientError raised during an SES send."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


class SESAdapter(ESPAdapter):
    name = "ses"

    def __init__(self, settings) -> None:
        self._settings = settings

    @functools.cached_property
    def _sesv2(self):
        return boto3.client("sesv2", region_name=self._settings.aws_region)

    @functools.cached_property
    def _ses(self):
        return boto3.client("ses", region_name=self._settings.aws_region)

    @functools.cached_property
    def _cw(self):
        return boto3.client("cloudwatch", region_name=self._settings.aws_region)

    # ------------------------------------------------------------------ send
    async def send(self, message: OutboundMessage) -> SendResult:
        raw_bytes = self._build_raw_mime(message)

        kwargs = {
            "FromEmailAddress": self._settings.ses_from_address,
            "Destination": {"ToAddresses": [message.to_email]},
            "Content": {"Raw": {"Data": raw_bytes}},
        }
        if self._settings.ses_configuration_set:
            kwargs["ConfigurationSetName"] = self._settings.ses_configuration_set
        tags = [{"Name": k, "Value": v} for k, v in message.message_tags.items()]
        if tags:
            kwargs["EmailTags"] = tags

        try:
            response = await asyncio.to_thread(self._sesv2.send_email, **kwargs)
        except ClientError as exc:
            logger.error(
                "ses_send_failed",
                to=message.to_email,
                error=str(exc),
            )
            raise SESError(exc) from exc

        return SendResult(esp_message_id=response["MessageId"], raw=response)

    def _build_raw_mime(self, message: OutboundMessage) -> bytes:
        msg = EmailMessage()
        msg["From"] = message.from_address
        msg["To"] = message.to_email
        msg["Subject"] = message.subject

        if message.list_unsubscribe:
            msg["List-Unsubscribe"] = f"<{message.list_unsubscribe}>"
            if message.list_unsubscribe_post:
                msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        # Plain text first, HTML second (multipart/alternative).
        msg.set_content(message.body_text or "")
        msg.add_alternative(message.body_html or "", subtype="html")

        return msg.as_bytes()

    # -------------------------------------------------------- get_reputation
    async def get_reputation(self) -> ReputationSnapshot:
        try:
            account = await asyncio.to_thread(self._sesv2.get_account)
            sending_enabled = bool(account.get("SendingEnabled", False))

            end = datetime.now(timezone.utc)
            start = end - timedelta(hours=24)

            bounce_rate = await self._metric_avg("Reputation.BounceRate", start, end)
            complaint_rate = await self._metric_avg(
                "Reputation.ComplaintRate", start, end
            )

            return ReputationSnapshot(
                account_sending_enabled=sending_enabled,
                bounce_rate_24h=bounce_rate,
                complaint_rate_24h=complaint_rate,
                raw={"account": account},
            )
        except ClientError as exc:
            logger.error("ses_get_reputation_failed", error=str(exc))
            return ReputationSnapshot(
                account_sending_enabled=False,
                bounce_rate_24h=None,
                complaint_rate_24h=None,
                raw={"error": str(exc)},
            )

    async def _metric_avg(
        self, metric_name: str, start: datetime, end: datetime
    ) -> float | None:
        try:
            resp = await asyncio.to_thread(
                self._cw.get_metric_statistics,
                Namespace="AWS/SES",
                MetricName=metric_name,
                StartTime=start,
                EndTime=end,
                Period=3600,
                Statistics=["Average"],
            )
        except ClientError as exc:
            logger.warning("ses_cw_metric_failed", metric=metric_name, error=str(exc))
            return None

        points = resp.get("Datapoints") or []
        if not points:
            return None
        avgs = [p["Average"] for p in points if "Average" in p]
        if not avgs:
            return None
        return sum(avgs) / len(avgs)

    # -------------------------------------------------------- parse_webhook
    def parse_webhook(
        self, payload: dict, signature: str | None
    ) -> list[ParsedEvent]:
        envelope_type = payload.get("Type")

        if envelope_type == "SubscriptionConfirmation":
            return []

        if envelope_type != "Notification":
            return []

        if signature is not None and getattr(
            self._settings, "sns_verify_signatures", False
        ):
            # TODO: implement full SNS cert-chain signature validation.
            payload_sig = payload.get("Signature")
            if payload_sig is None:
                logger.warning("sns_missing_signature_field")

        raw_message = payload.get("Message")
        if not isinstance(raw_message, str):
            return []

        try:
            msg = json.loads(raw_message)
        except (ValueError, TypeError):
            logger.warning("sns_message_not_json")
            return []

        ses_type = (
            msg.get("notificationType") or msg.get("eventType") or ""
        ).lower()

        mail = msg.get("mail") or {}
        esp_message_id = mail.get("messageId")
        destination = mail.get("destination") or []
        recipient_email = destination[0] if destination else None

        ts = mail.get("timestamp")
        if isinstance(ts, str):
            occurred_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            occurred_at = datetime.now(timezone.utc)

        event_type: str | None = None
        event_subtype: str | None = None
        url: str | None = None

        if ses_type == "delivery":
            event_type = "delivery"
        elif ses_type == "bounce":
            event_type = "bounce"
            bounce = msg.get("bounce") or {}
            bounce_type = bounce.get("bounceType")
            event_subtype = _BOUNCE_SUBTYPE_MAP.get(
                bounce_type, (bounce_type or "").lower() or None
            )
            bounced = bounce.get("bouncedRecipients") or []
            if bounced and bounced[0].get("emailAddress"):
                recipient_email = bounced[0]["emailAddress"]
        elif ses_type == "complaint":
            event_type = "complaint"
            complaint = msg.get("complaint") or {}
            event_subtype = complaint.get("complaintFeedbackType")
        elif ses_type == "open":
            event_type = "open"
        elif ses_type == "click":
            event_type = "click"
            click = msg.get("click") or {}
            url = click.get("link")
        elif ses_type == "reject":
            event_type = "reject"
        else:
            return []

        return [
            ParsedEvent(
                event_type=event_type,
                event_subtype=event_subtype,
                esp_message_id=esp_message_id,
                recipient_email=recipient_email,
                occurred_at=occurred_at,
                url=url,
                user_agent=None,
                raw=msg,
            )
        ]

    # ------------------------------------------------ add_to_esp_suppression
    async def add_to_esp_suppression(self, email: str, reason: str) -> None:
        normalized = (reason or "").lower()
        if normalized in ("hard_bounce", "bounce"):
            mapped = "BOUNCE"
        elif normalized == "complaint":
            mapped = "COMPLAINT"
        else:
            raise ValueError(f"Unsupported SES suppression reason: {reason!r}")

        try:
            await asyncio.to_thread(
                self._sesv2.put_suppressed_destination,
                EmailAddress=email,
                Reason=mapped,
            )
        except ClientError as exc:
            logger.warning(
                "ses_put_suppressed_destination_failed",
                email=email,
                reason=mapped,
                error=str(exc),
            )
