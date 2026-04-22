from __future__ import annotations

from app.adapters.alerts.base import AlertDispatcher, AlertEvent
from app.core.config import get_settings


def get_alert_dispatcher() -> AlertDispatcher:
    settings = get_settings()
    if settings.slack_webhook_url:
        from app.adapters.alerts.slack import SlackAlertDispatcher
        return SlackAlertDispatcher()
    from app.adapters.alerts.noop import NoopAlertDispatcher
    return NoopAlertDispatcher()


__all__ = ["AlertDispatcher", "AlertEvent", "get_alert_dispatcher"]
