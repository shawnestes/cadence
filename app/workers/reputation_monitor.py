"""Reputation monitor: evaluates bounce/complaint rates and manages alert lifecycle.

Runs on an APScheduler IntervalTrigger. For each monitored tenant + metric:
- Below threshold + no open alert  → nothing
- Below threshold + open alert     → resolve, dispatch "resolved"
- Above threshold + no open alert  → create pending row
- Above threshold + pending alert  → promote to active after sustained_minutes
- Above threshold + active alert   → update severity in-place, no re-dispatch
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.alerts import AlertDispatcher, AlertEvent, get_alert_dispatcher
from app.api.stats import _compute_topline
from app.core.config import get_settings
from app.core.db import async_session_factory
from app.models.alert import Alert
from app.models.base import uuid7

log = structlog.get_logger(__name__)

_METRICS = ("bounce_rate", "complaint_rate")


def _thresholds(metric: str) -> tuple[float, float]:
    """Return (warn_threshold, critical_threshold) for a metric."""
    s = get_settings()
    if metric == "bounce_rate":
        return s.alert_bounce_warn_rate, s.alert_bounce_critical_rate
    if metric == "complaint_rate":
        return s.alert_complaint_warn_rate, s.alert_complaint_critical_rate
    raise ValueError(f"unknown metric: {metric}")


def _get_value(topline: Any, metric: str) -> float:
    return float(getattr(topline, metric))


async def _run_tenant_metric(
    db: AsyncSession,
    tenant: str,
    metric: str,
    dispatcher: AlertDispatcher,
    now: datetime,
) -> None:
    s = get_settings()
    warn_thresh, critical_thresh = _thresholds(metric)

    topline = await _compute_topline(db, tenant, s.alert_eval_window)
    value = _get_value(topline, metric)

    if value >= critical_thresh:
        severity = "critical"
        threshold = critical_thresh
    elif value >= warn_thresh:
        severity = "warn"
        threshold = warn_thresh
    else:
        severity = None
        threshold = warn_thresh  # fallback, unused

    # Look up open alerts for this tenant+metric
    stmt = (
        select(Alert)
        .where(
            and_(
                Alert.tenant == tenant,
                Alert.metric == metric,
                Alert.status.in_(("pending", "active")),
            )
        )
        .order_by(Alert.created_at.asc())
        .limit(1)
    )
    existing: Alert | None = (await db.execute(stmt)).scalar_one_or_none()

    if severity is None:
        # Below threshold
        if existing is None:
            return
        # Resolve the open alert
        existing.status = "resolved"
        existing.resolved_at = now
        existing.notes = f"recovered to {value:.6f}"
        existing.updated_at = now
        await db.commit()
        event = AlertEvent(
            alert_id=existing.id,
            tenant=tenant,
            metric=metric,
            severity=existing.severity,
            value=value,
            threshold=existing.threshold,
            status_transition="resolved",
        )
        try:
            await dispatcher.notify(event)
        except Exception:  # noqa: BLE001
            log.warning("reputation_monitor.dispatch_error", alert_id=str(existing.id))
        log.info(
            "reputation_monitor.resolved",
            tenant=tenant,
            metric=metric,
            value=value,
        )
        return

    # Above threshold
    if existing is None:
        # Create new pending alert
        alert = Alert(
            id=uuid7(),
            tenant=tenant,
            metric=metric,
            severity=severity,
            threshold=threshold,
            value=value,
            status="pending",
            pending_since=now,
        )
        db.add(alert)
        await db.commit()
        log.info(
            "reputation_monitor.pending",
            tenant=tenant,
            metric=metric,
            severity=severity,
            value=value,
            threshold=threshold,
        )
        return

    # Update severity if upgraded
    severity_rank = {"warn": 0, "critical": 1}
    if severity_rank.get(severity, 0) > severity_rank.get(existing.severity, 0):
        existing.severity = severity
        existing.threshold = threshold
        existing.value = value
        existing.updated_at = now

    if existing.status == "pending":
        ps = existing.pending_since
        if ps.tzinfo is None:
            ps = ps.replace(tzinfo=timezone.utc)
        elapsed = now - ps
        if elapsed >= timedelta(minutes=s.alert_sustained_minutes):
            existing.status = "active"
            existing.activated_at = now
            existing.updated_at = now
            await db.commit()
            event = AlertEvent(
                alert_id=existing.id,
                tenant=tenant,
                metric=metric,
                severity=existing.severity,
                value=value,
                threshold=existing.threshold,
                status_transition="activated",
            )
            try:
                await dispatcher.notify(event)
            except Exception:  # noqa: BLE001
                log.warning("reputation_monitor.dispatch_error", alert_id=str(existing.id))
            log.info(
                "reputation_monitor.activated",
                tenant=tenant,
                metric=metric,
                severity=existing.severity,
                value=value,
            )
        else:
            existing.value = value
            existing.updated_at = now
            await db.commit()
            log.info(
                "reputation_monitor.still_pending",
                tenant=tenant,
                metric=metric,
                elapsed_seconds=elapsed.total_seconds(),
            )
    else:
        # Already active — update value, severity already updated above
        existing.value = value
        existing.updated_at = now
        await db.commit()
        log.info(
            "reputation_monitor.active_updated",
            tenant=tenant,
            metric=metric,
            severity=existing.severity,
            value=value,
        )


async def run_once(dispatcher: AlertDispatcher | None = None) -> None:
    s = get_settings()
    if dispatcher is None:
        dispatcher = get_alert_dispatcher()

    now = datetime.now(timezone.utc)

    for tenant in s.alert_tenants:
        for metric in _METRICS:
            try:
                async with async_session_factory() as db:
                    await _run_tenant_metric(db, tenant, metric, dispatcher, now)
            except Exception:  # noqa: BLE001
                log.exception(
                    "reputation_monitor.error",
                    tenant=tenant,
                    metric=metric,
                )
