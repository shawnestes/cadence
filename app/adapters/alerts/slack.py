from __future__ import annotations

import structlog

from app.adapters.alerts.base import AlertEvent
from app.core.config import get_settings

log = structlog.get_logger(__name__)


class SlackAlertDispatcher:
    async def notify(self, alert: AlertEvent) -> None:
        settings = get_settings()
        webhook_url = settings.slack_webhook_url

        if not webhook_url:
            log.info(
                "alert.dispatcher.slack.no_op",
                reason="webhook_url_not_configured",
                alert_id=str(alert.alert_id),
                metric=alert.metric,
            )
            return

        transition_label = "ACTIVATED" if alert.status_transition == "activated" else "RESOLVED"
        severity_emoji = "[CRITICAL]" if alert.severity == "critical" else "[WARN]"
        text = (
            f"{severity_emoji} Alert {transition_label} | "
            f"tenant={alert.tenant} metric={alert.metric} "
            f"value={alert.value:.4f} threshold={alert.threshold:.4f} "
            f"severity={alert.severity}"
        )

        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(webhook_url, json={"text": text})
                resp.raise_for_status()
                log.info(
                    "alert.dispatcher.slack.sent",
                    alert_id=str(alert.alert_id),
                    metric=alert.metric,
                    transition=alert.status_transition,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "alert.dispatcher.slack.failed",
                alert_id=str(alert.alert_id),
                error=str(exc),
            )
