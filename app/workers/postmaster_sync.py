"""Postmaster sync: pull Gmail's reported metrics, store them, alert on spam rate.

Gmail publishes no standard email feedback loop, so a complaint from a Gmail
recipient never reaches SES and never becomes a `contact_event`. The only
place that signal exists is Postmaster Tools. Without this worker the
0.1%/0.3% thresholds that decide Gmail deliverability are unobservable, and
399 of 500 recipients on the OTTO list are Gmail.

Every reading is persisted to `postmaster_daily`, not just threshold breaches.
Alerting alone records nothing on a healthy day, which would leave no baseline
and no way to correlate reputation against the warm-up ramp -- the reason for
ramping in the first place.

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
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.adapters.alerts import get_alert_dispatcher
from app.adapters.postmaster import (
    SPAM_RATE_METRIC,
    DailyMetric,
    PostmasterError,
    get_postmaster_client,
)
from app.core.config import get_settings
from app.core.db import async_session_factory
from app.models.postmaster_daily import PostmasterDaily
from app.workers.reputation_monitor import _apply_alert_state

log = structlog.get_logger(__name__)

# Namespaced so it cannot collide with the SES-derived complaint_rate: the two
# measure different things from different sources and must alert independently.
METRIC = "gmail_spam_rate"

# Stored for history even though only SPAM_RATE drives an alert. Support varies
# by domain and volume, and an unsupported metric 400s the request, so each is
# fetched separately and a failure on one does not lose the others.
TRACKED_METRICS = (
    SPAM_RATE_METRIC,
    "DELIVERY_ERROR_RATE",
    "DELIVERY_ERROR_COUNT",
)


async def _store(rows: list[DailyMetric], domain: str) -> int:
    """Upsert readings. Postmaster restates recent days as data arrives and the
    sync re-reads a lookback window every run, so conflict means overwrite."""
    if not rows:
        return 0
    async with async_session_factory() as db:
        stmt = pg_insert(PostmasterDaily).values([
            {
                "domain": domain,
                "day": r.day,
                "metric": r.metric,
                "value": r.value,
            }
            for r in rows
        ])
        stmt = stmt.on_conflict_do_update(
            index_elements=["domain", "day", "metric"],
            set_={
                "value": stmt.excluded.value,
                "fetched_at": datetime.now(timezone.utc),
            },
        )
        await db.execute(stmt)
        await db.commit()
    return len(rows)


async def run_once() -> dict[str, int]:
    """Fetch recent daily metrics, persist them, evaluate the newest spam rate."""
    settings = get_settings()
    client = get_postmaster_client()
    if client is None:
        return {"checked": 0, "days": 0, "stored": 0}

    domain = settings.postmaster_domain
    if not domain:
        # Fall back to the single registered domain. More than one is ambiguous
        # -- alerting on an arbitrary pick would be worse than not alerting.
        try:
            registered = await client.domains()
        except PostmasterError as exc:
            log.error("postmaster_sync.domains_failed", error=str(exc))
            return {"checked": 0, "days": 0, "stored": 0}
        if len(registered) != 1:
            log.error(
                "postmaster_sync.domain_ambiguous",
                count=len(registered),
                hint="set CADENCE_POSTMASTER_DOMAIN",
            )
            return {"checked": 0, "days": 0, "stored": 0}
        domain = registered[0]

    end = date.today()
    start = end - timedelta(days=max(settings.postmaster_lookback_days, 1))

    spam_rows: list[DailyMetric] = []
    stored = 0
    for metric in TRACKED_METRICS:
        try:
            rows = await client.daily_metric(domain, metric, start, end)
        except PostmasterError as exc:
            # Unsupported-at-this-volume metrics 400. Losing one must not cost
            # the others, and must never cost the spam rate.
            log.warning(
                "postmaster_sync.metric_failed",
                domain=domain,
                metric=metric,
                error=str(exc),
            )
            continue
        stored += await _store(rows, domain)
        if metric == SPAM_RATE_METRIC:
            spam_rows = rows

    if not spam_rows:
        # Postmaster is volume-gated: below a daily threshold it reports
        # nothing at all. Absent data is not a healthy zero, so do not touch
        # the alert state -- an open alert must not silently resolve because
        # volume dropped.
        log.info(
            "postmaster_sync.no_data",
            domain=domain,
            start=start.isoformat(),
            end=end.isoformat(),
            stored=stored,
        )
        return {"checked": 0, "days": 0, "stored": stored}

    latest = spam_rows[-1]
    tenant = settings.alert_tenants[0] if settings.alert_tenants else "otto"
    now = datetime.now(timezone.utc)

    log.info(
        "postmaster_sync.spam_rate",
        domain=domain,
        day=latest.day.isoformat(),
        value=latest.value,
        days_returned=len(spam_rows),
        stored=stored,
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

    return {"checked": 1, "days": len(spam_rows), "stored": stored}
