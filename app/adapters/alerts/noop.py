from __future__ import annotations

import structlog

from app.adapters.alerts.base import AlertEvent

log = structlog.get_logger(__name__)


class NoopAlertDispatcher:
    async def notify(self, alert: AlertEvent) -> None:
        log.info(
            "alert.dispatcher.noop",
            alert_id=str(alert.alert_id),
            tenant=alert.tenant,
            metric=alert.metric,
            severity=alert.severity,
            value=alert.value,
            threshold=alert.threshold,
            status_transition=alert.status_transition,
        )
