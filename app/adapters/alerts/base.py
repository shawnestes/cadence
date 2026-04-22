from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID


@dataclass(slots=True)
class AlertEvent:
    alert_id: UUID
    tenant: str
    metric: str
    severity: str
    value: float
    threshold: float
    status_transition: Literal["activated", "resolved"]


class AlertDispatcher(Protocol):
    async def notify(self, alert: AlertEvent) -> None: ...
