import uuid
from datetime import datetime, timezone

import structlog

from app.adapters.esp.base import (
    ESPAdapter,
    OutboundMessage,
    ParsedEvent,
    ReputationSnapshot,
    SendResult,
)

logger = structlog.get_logger(__name__)


class NoopESP(ESPAdapter):
    name = "noop"

    async def send(self, message: OutboundMessage) -> SendResult:
        message_id = f"noop-{uuid.uuid4()}"
        logger.info(
            "noop_esp_send",
            to=message.to_email,
            from_=message.from_address,
            subject=message.subject,
            message_id=message_id,
        )
        return SendResult(esp_message_id=message_id, raw={"noop": True})

    async def get_reputation(self) -> ReputationSnapshot:
        return ReputationSnapshot(
            account_sending_enabled=True,
            bounce_rate_24h=0.0,
            complaint_rate_24h=0.0,
            raw={"noop": True},
        )

    def parse_webhook(self, payload: dict, signature: str | None) -> list[ParsedEvent]:
        occurred_at_raw = payload.get("occurred_at")
        if isinstance(occurred_at_raw, datetime):
            occurred_at = occurred_at_raw
        elif isinstance(occurred_at_raw, str):
            occurred_at = datetime.fromisoformat(occurred_at_raw.replace("Z", "+00:00"))
        else:
            occurred_at = datetime.now(timezone.utc)

        return [
            ParsedEvent(
                event_type=payload.get("event_type", "delivery"),
                event_subtype=payload.get("event_subtype"),
                esp_message_id=payload.get("message_id"),
                recipient_email=payload.get("email"),
                occurred_at=occurred_at,
                url=payload.get("url"),
                user_agent=payload.get("user_agent"),
                raw=payload,
            )
        ]

    async def add_to_esp_suppression(self, email: str, reason: str) -> None:
        logger.info("noop_esp_suppression", email=email, reason=reason)
