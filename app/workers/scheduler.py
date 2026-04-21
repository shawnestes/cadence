from __future__ import annotations

import asyncio
import signal

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.workers import otto_sync


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
    scheduler.start()
    log.info("scheduler.started", jobs=[j.id for j in scheduler.get_jobs()])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    scheduler.shutdown(wait=True)
    log.info("scheduler.stopped")


if __name__ == "__main__":
    asyncio.run(main())
