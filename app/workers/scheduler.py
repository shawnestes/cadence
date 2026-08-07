from __future__ import annotations

import asyncio
import signal

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.workers import (
    engagement_scorer,
    otto_sync,
    partition_maintenance,
    profile_refresh,
    reputation_checks,
    reputation_monitor,
    send_worker,
    sqs_event_poller,
    stats_rollup,
    variant_retirement,
    workflow_tick,
)


async def main() -> None:
    configure_logging(get_settings().log_level)
    log = structlog.get_logger(__name__)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        otto_sync.run_once,
        "interval",
        seconds=60,
        id="otto_unsub_sync",
        max_instances=1,
    )
    scheduler.add_job(
        send_worker.run_once,
        "interval",
        seconds=1,
        id="send_worker",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        sqs_event_poller.run_once,
        "interval",
        seconds=5,
        id="sqs_event_poller",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        workflow_tick.run_once,
        "interval",
        seconds=get_settings().workflow_tick_interval_seconds,
        id="workflow_tick",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        stats_rollup.run_once,
        "interval",
        seconds=get_settings().stats_refresh_interval_seconds,
        id="stats_rollup",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        variant_retirement.run_once,
        "interval",
        hours=6,
        id="variant_retirement",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reputation_monitor.run_once,
        "interval",
        seconds=get_settings().alert_monitor_interval_seconds,
        id="reputation_monitor",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reputation_checks.run_once,
        "interval",
        seconds=get_settings().alert_monitor_interval_seconds,
        id="reputation_checks",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        partition_maintenance.run_once,
        "interval",
        hours=24,
        id="partition_maintenance",
        max_instances=1,
        coalesce=True,
    )
    # Retired in M-SEG-7 — no-op stub; slot retained to avoid config drift.
    scheduler.add_job(
        engagement_scorer.run_once,
        "cron",
        hour=3,
        id="engagement_scorer",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        profile_refresh.run_incremental,
        'interval',
        seconds=120,
        id='profile_refresh_incremental',
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        profile_refresh.run_nightly,
        'cron',
        hour=3,
        minute=30,
        id='profile_refresh_nightly',
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("scheduler.started", jobs=[j.id for j in scheduler.get_jobs()])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows event loops don't support signal handlers; rely on
            # KeyboardInterrupt / process termination instead.
            break

    await stop.wait()
    scheduler.shutdown(wait=True)
    log.info("scheduler.stopped")


if __name__ == "__main__":
    asyncio.run(main())
