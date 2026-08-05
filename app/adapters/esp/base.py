from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class OutboundMessage:
    to_email: str
    from_address: str
    subject: str
    body_html: str
    body_text: str
    configuration_set: str | None = None
    list_unsubscribe: str | None = None
    list_unsubscribe_post: bool = True
    message_tags: dict[str, str] = field(default_factory=dict)
    feedback_id: str | None = None


@dataclass(slots=True)
class SendResult:
    esp_message_id: str
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class ParsedEvent:
    event_type: str
    event_subtype: str | None
    esp_message_id: str | None
    recipient_email: str | None
    occurred_at: datetime
    url: str | None = None
    user_agent: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class IspReputationMetric:
    """Per-ISP reputation signal pulled from CloudWatch for a managed IP pool."""

    isp: str
    reputation: float | None
    effective_daily_rate: float | None


@dataclass(slots=True)
class ReputationSnapshot:
    account_sending_enabled: bool
    bounce_rate_24h: float | None
    complaint_rate_24h: float | None
    # Managed Dedicated IP pool signals (addendum §2.4). None when no pool is
    # configured or CloudWatch is unreachable.
    managed_sending_allowed: bool | None = None
    managed_dedicated_ip_count: float | None = None
    isp_metrics: list[IspReputationMetric] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class ESPAdapter(ABC):
    name: str

    @abstractmethod
    async def send(self, message: OutboundMessage) -> SendResult: ...

    @abstractmethod
    async def get_reputation(self) -> ReputationSnapshot: ...

    @abstractmethod
    def parse_webhook(self, payload: dict, signature: str | None) -> list[ParsedEvent]: ...

    @abstractmethod
    async def add_to_esp_suppression(self, email: str, reason: str) -> None: ...
