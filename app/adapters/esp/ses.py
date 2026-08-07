from __future__ import annotations

import asyncio
import base64
import functools
import json
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import boto3
import httpx
import structlog
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate

from app.adapters.esp.base import (
    ESPAdapter,
    IspReputationMetric,
    OutboundMessage,
    ParsedEvent,
    ReputationSnapshot,
    SendResult,
)

if TYPE_CHECKING:
    from app.models.sending_identity import SendingIdentity

logger = structlog.get_logger(__name__)

# AWS creds may not be present in every environment (e.g. local dev, CI).
# Keep CloudWatch/SES calls fast-failing so a missing/unreachable AWS account
# never hangs a request path — callers treat ClientError/BotoCoreError as
# "no data available" and degrade to None rather than raising.
_AWS_CLIENT_CONFIG = BotoConfig(
    connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}
)

# Dimension name AWS SES uses to scope managed-pool CloudWatch metrics.
_POOL_DIMENSION_NAME = "ses:dedicated-ip-pool-name"
_ISP_DIMENSION_NAME = "isp"


_BOUNCE_SUBTYPE_MAP = {
    "Permanent": "hard",
    "Transient": "soft",
    "Undetermined": "soft",
}


# --- SNS signature verification ---------------------------------------------

# In-process cert cache. Keyed by SigningCertURL. Small for a single publisher.
_SNS_CERT_CACHE: dict[str, bytes] = {}


def _is_allowed_cert_host(url: str) -> bool:
    """Allow only AWS-owned SNS cert hosts to prevent SSRF."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # AWS publishes SNS signing certs under sns.<region>.amazonaws.com. We
    # allow any *.amazonaws.com host; the cert itself authenticates the
    # publisher.
    return host.endswith(".amazonaws.com")


def _canonical_string_for_notification(msg: dict) -> str:
    """Build the SNS canonical string for Notification messages.

    Per https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
    fields must be in a specific order and include Subject only if present.
    """
    parts = [
        "Message", msg.get("Message", ""),
        "MessageId", msg.get("MessageId", ""),
    ]
    if msg.get("Subject") is not None:
        parts += ["Subject", msg["Subject"]]
    parts += [
        "Timestamp", msg.get("Timestamp", ""),
        "TopicArn", msg.get("TopicArn", ""),
        "Type", msg.get("Type", ""),
    ]
    return "\n".join(parts) + "\n"


def _canonical_string_for_subscription(msg: dict) -> str:
    parts = [
        "Message", msg.get("Message", ""),
        "MessageId", msg.get("MessageId", ""),
        "SubscribeURL", msg.get("SubscribeURL", ""),
        "Timestamp", msg.get("Timestamp", ""),
        "Token", msg.get("Token", ""),
        "TopicArn", msg.get("TopicArn", ""),
        "Type", msg.get("Type", ""),
    ]
    return "\n".join(parts) + "\n"


def _verify_sns_signature(msg: dict) -> bool:
    """Validate an SNS message signature. Returns True on success."""
    cert_url = msg.get("SigningCertURL") or msg.get("SigningCertUrl")
    signature_b64 = msg.get("Signature")
    sig_version = msg.get("SignatureVersion", "1")
    msg_type = msg.get("Type")

    if not cert_url or not signature_b64 or not msg_type:
        logger.warning("sns_signature_missing_fields")
        return False

    if not _is_allowed_cert_host(cert_url):
        logger.warning("sns_signing_cert_host_not_allowed", url=cert_url)
        return False

    # Fetch + cache the cert PEM.
    pem = _SNS_CERT_CACHE.get(cert_url)
    if pem is None:
        try:
            resp = httpx.get(cert_url, timeout=5.0)
            resp.raise_for_status()
            pem = resp.content
        except httpx.HTTPError as exc:
            logger.warning("sns_signing_cert_fetch_failed", error=str(exc))
            return False
        _SNS_CERT_CACHE[cert_url] = pem

    try:
        cert = load_pem_x509_certificate(pem)
        public_key = cert.public_key()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sns_signing_cert_parse_failed", error=str(exc))
        return False

    if msg_type == "Notification":
        canonical = _canonical_string_for_notification(msg)
    elif msg_type in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
        canonical = _canonical_string_for_subscription(msg)
    else:
        logger.warning("sns_unknown_message_type", type=msg_type)
        return False

    try:
        signature = base64.b64decode(signature_b64)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sns_signature_b64_decode_failed", error=str(exc))
        return False

    algo = hashes.SHA256() if sig_version == "2" else hashes.SHA1()
    try:
        # SNS signs with RSASSA-PKCS1-v1_5.
        public_key.verify(
            signature,
            canonical.encode("utf-8"),
            padding.PKCS1v15(),
            algo,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("sns_signature_verify_failed", error=str(exc))
        return False


class SESError(Exception):
    """Wraps a botocore ClientError raised during an SES send."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


class SESAdapter(ESPAdapter):
    name = "ses"

    def __init__(self, settings, identity: "SendingIdentity | None" = None) -> None:
        """`identity`, when given, is a `SendingIdentity` (SPEC_ADDENDUM_003
        §3) this adapter is configured *from*: from-address, region,
        configuration set, and managed-pool name come off
        `identity.provider_config`, falling back to `settings` for any key
        the identity doesn't set. `identity=None` keeps the pre-addendum
        settings-only behavior for the legacy call sites.

        `provider_config.role_arn` / `account_key` are reserved for
        cross-account AssumeRole routing -- deferred per §9, deliberately
        not read here. Every identity uses the ambient AWS credentials until
        a second SES account lands.
        """
        self._settings = settings
        self._identity = identity

    @property
    def _provider_config(self) -> dict:
        if self._identity is None:
            return {}
        return self._identity.provider_config or {}

    @property
    def _from_address(self) -> str | None:
        if self._identity is not None:
            return self._identity.from_address
        return self._settings.ses_from_address

    @property
    def _region(self) -> str:
        return self._provider_config.get("region") or self._settings.aws_region

    @property
    def _configuration_set(self) -> str | None:
        return (
            self._provider_config.get("configuration_set")
            or self._settings.ses_configuration_set
        )

    @property
    def _managed_pool_name(self) -> str | None:
        return self._provider_config.get("managed_pool_name") or getattr(
            self._settings, "ses_managed_pool_name", None
        )

    @functools.cached_property
    def _sesv2(self):
        return boto3.client(
            "sesv2", region_name=self._region, config=_AWS_CLIENT_CONFIG
        )

    @functools.cached_property
    def _ses(self):
        return boto3.client(
            "ses", region_name=self._region, config=_AWS_CLIENT_CONFIG
        )

    @functools.cached_property
    def _cw(self):
        return boto3.client(
            "cloudwatch", region_name=self._region, config=_AWS_CLIENT_CONFIG
        )

    # ------------------------------------------------------------------ send
    async def send(self, message: OutboundMessage) -> SendResult:
        raw_bytes = self._build_raw_mime(message)

        kwargs = {
            "FromEmailAddress": self._from_address,
            "Destination": {"ToAddresses": [message.to_email]},
            "Content": {"Raw": {"Data": raw_bytes}},
        }
        if self._configuration_set:
            kwargs["ConfigurationSetName"] = self._configuration_set
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

        if message.feedback_id:
            msg["Feedback-ID"] = message.feedback_id

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

            managed_sending_allowed: bool | None = None
            managed_dedicated_ip_count: float | None = None
            isp_metrics: list[IspReputationMetric] = []

            pool_name = self._managed_pool_name
            if pool_name:
                managed_dedicated_ip_count = await self._metric_avg(
                    "Managed.DedicatedIpCount",
                    start,
                    end,
                    dimensions=[
                        {"Name": _POOL_DIMENSION_NAME, "Value": pool_name}
                    ],
                )
                sending_allowed_val = await self._metric_avg(
                    "Managed.SendingAllowed",
                    start,
                    end,
                    dimensions=[
                        {"Name": _POOL_DIMENSION_NAME, "Value": pool_name}
                    ],
                )
                if sending_allowed_val is not None:
                    managed_sending_allowed = bool(sending_allowed_val)
                isp_metrics = await self._isp_feedback_metrics(pool_name, start, end)

            return ReputationSnapshot(
                account_sending_enabled=sending_enabled,
                bounce_rate_24h=bounce_rate,
                complaint_rate_24h=complaint_rate,
                managed_sending_allowed=managed_sending_allowed,
                managed_dedicated_ip_count=managed_dedicated_ip_count,
                isp_metrics=isp_metrics,
                raw={"account": account},
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("ses_get_reputation_failed", error=str(exc))
            return ReputationSnapshot(
                account_sending_enabled=False,
                bounce_rate_24h=None,
                complaint_rate_24h=None,
                raw={"error": str(exc)},
            )

    async def _metric_avg(
        self,
        metric_name: str,
        start: datetime,
        end: datetime,
        dimensions: list[dict] | None = None,
    ) -> float | None:
        try:
            resp = await asyncio.to_thread(
                self._cw.get_metric_statistics,
                Namespace="AWS/SES",
                MetricName=metric_name,
                Dimensions=dimensions or [],
                StartTime=start,
                EndTime=end,
                Period=3600,
                Statistics=["Average"],
            )
        except (ClientError, BotoCoreError) as exc:
            logger.warning("ses_cw_metric_failed", metric=metric_name, error=str(exc))
            return None

        points = resp.get("Datapoints") or []
        if not points:
            return None
        avgs = [p["Average"] for p in points if "Average" in p]
        if not avgs:
            return None
        return sum(avgs) / len(avgs)

    async def _isp_feedback_metrics(
        self, pool_name: str, start: datetime, end: datetime
    ) -> list[IspReputationMetric]:
        """Pull per-ISP `Reputation.IspFeedback` datapoints for the managed pool.

        AWS doesn't publish a fixed list of ISP dimension values, so we
        discover which ISPs currently have data via `list_metrics`, then pull
        each one's series individually. `reputation` is the windowed average;
        `effective_daily_rate` is the most recent datapoint, treated as the
        ISP's current effective sending-rate signal.
        """
        try:
            listing = await asyncio.to_thread(
                self._cw.list_metrics,
                Namespace="AWS/SES",
                MetricName="Reputation.IspFeedback",
                Dimensions=[{"Name": _POOL_DIMENSION_NAME, "Value": pool_name}],
            )
        except (ClientError, BotoCoreError) as exc:
            logger.warning("ses_cw_list_isp_metrics_failed", error=str(exc))
            return []

        metrics_meta = listing.get("Metrics") or []
        results: list[IspReputationMetric] = []
        for metric in metrics_meta:
            dims = metric.get("Dimensions") or []
            isp_name = next(
                (d["Value"] for d in dims if d.get("Name") == _ISP_DIMENSION_NAME),
                None,
            )
            if not isp_name:
                continue

            try:
                resp = await asyncio.to_thread(
                    self._cw.get_metric_statistics,
                    Namespace="AWS/SES",
                    MetricName="Reputation.IspFeedback",
                    Dimensions=dims,
                    StartTime=start,
                    EndTime=end,
                    Period=3600,
                    Statistics=["Average"],
                )
            except (ClientError, BotoCoreError) as exc:
                logger.warning(
                    "ses_cw_isp_metric_failed", isp=isp_name, error=str(exc)
                )
                results.append(
                    IspReputationMetric(
                        isp=isp_name, reputation=None, effective_daily_rate=None
                    )
                )
                continue

            points = sorted(
                (p for p in (resp.get("Datapoints") or []) if "Average" in p),
                key=lambda p: p["Timestamp"],
            )
            if not points:
                results.append(
                    IspReputationMetric(
                        isp=isp_name, reputation=None, effective_daily_rate=None
                    )
                )
                continue

            reputation = sum(p["Average"] for p in points) / len(points)
            effective_daily_rate = points[-1]["Average"]
            results.append(
                IspReputationMetric(
                    isp=isp_name,
                    reputation=reputation,
                    effective_daily_rate=effective_daily_rate,
                )
            )

        return results

    # -------------------------------------------------------- parse_webhook
    def parse_webhook(
        self, payload: dict, signature: str | None
    ) -> list[ParsedEvent]:
        envelope_type = payload.get("Type")

        if envelope_type == "SubscriptionConfirmation":
            return []

        if envelope_type != "Notification":
            return []

        if getattr(self._settings, "sns_verify_signatures", False):
            if not _verify_sns_signature(payload):
                logger.warning(
                    "sns_signature_invalid",
                    envelope_type=envelope_type,
                )
                return []

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
