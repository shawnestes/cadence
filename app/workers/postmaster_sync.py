"""Postmaster sync: pull Gmail's reported spam rate and alert on it.

Gmail publishes no standard email feedback loop, so a complaint from a Gmail
recipient never reaches SES and never becomes a `contact_event`. The only
place that signal exists is Postmaster Tools. Without this worker the
0.1%/0.3% thresholds that decide Gmail deliverability are unobservable, and
399 of 500 recipients on the OTTO list are Gmail.

The alert lifecycle (pending -> active -> resolved, sustained-minutes
promotion, severity upgrade in place) is reputation_monitor's; this worker only
supplies a value for a new metric name. One implementation, one set of
semantics, one place to fix a bug.

No-ops when no credentials are configured, so the scheduler can register it
unconditionally -- same contract as the SQS poller with no queue URL.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import structlog

from app.adapters.alerts import get_alert_dispatcher
from app.adapters.postmaster import (
    SPAM_RATE_METRIC,
    PostmasterError,
    get_postmaster_client,
)
from app.core.config import get_settings
from app.core.db import async_session_factory
from app.workers.reputation_monitor import _apply_alert_state

log = structlog.get_logger(__name__)

# Namespaced so it cannot collide with the SES-derived complaint_rate: the two
# measure different things from different sources and must alert independently.
METRIC = "gmail_spam_rate"


async def run_once() -> dict[str, int]:
    """Fetch recent daily spam rates and evaluate the newest against thresholds."""
    settings = get_settings()
    client = get_postmaster_client()
    if client is None:
        return {"checked": 0, "days": 0}

    domain = settings.postmaster_domain
    if not domain:
        # Fall back to the single registered domain. More than one is ambiguous
        # -- alerting on an arbitrary pick would be worse than not alerting.
        try:
            registered = await client.domains()
        except PostmasterError as exc:
            log.error("postmaster_sync.domains_failed", error=str(exc))
            return {"checked": 0, "days": 0}
        if len(registered) != 1:
            log.error(
                "postmaster_sync.domain_ambiguous",
                count=len(registered),
                hint="set CADENCE_POSTMASTER_DOMAIN",
            )
            return {"checked": 0, "days": 0}
        domain = registered[0]

    end = date.today()
    start = end - timedelta(days=max(settings.postmaster_lookback_days, 1))

    try:
        rows = await client.daily_metric(domain, SPAM_RATE_METRIC, start, end)
    except PostmasterError as exc:
        log.error("postmaster_sync.query_failed", domain=domain, error=str(exc))
        return {"checked": 0, "days": 0}

    if not rows:
        # Postmaster is volume-gated: below a daily threshold it reports
        # nothing at all. Absent data is not a healthy zero, so do not touch
        # the alert state -- an open alert must not silently resolve because
        # volume dropped.
        log.info(
            "postmaster_sync.no_data",
            domain=domain,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        return {"checked": 0, "days": 0}

    latest = rows[-1]
    tenant = settings.alert_tenants[0] if settings.alert_tenants else "otto"
    now = datetime.now(timezone.utc)

    log.info(
        "postmaster_sync.spam_rate",
        domain=domain,
        day=latest.day.isoformat(),
        value=latest.value,
        days_returned=len(rows),
        warn=settings.postmaster_spam_rate_warn,
        critical=settings.postmaster_spam_rate_critical,
    )

    async with async_session_factory() as db:
        await _apply_alert_state(
            db,
            tenant,
            METRIC,
            latest.value,
            settings.postmaster_spam_rate_warn,
            settings.postmaster_spam_rate_critical,
            get_alert_dispatcher(),
            now,
        )

    return {"checked": 1, "days": len(rows)}
