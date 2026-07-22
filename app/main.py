from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.api import (
    alerts,
    campaigns,
    health,
    ingest,
    metrics,
    recipients,
    stats,
    suppressions,
    templates,
    unsub,
    webhooks,
    workflows,
)
from app.core.auth import require_api_key
from app.core.config import get_settings
from app.core.db import engine
from app.core.logging import configure_logging
from app.core.partitions import ensure_events_partitions

configure_logging(get_settings().log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await ensure_events_partitions(conn)
    yield


app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(webhooks.router)
app.include_router(unsub.router)
app.include_router(
    ingest.router,
    prefix="/api",
    dependencies=[Depends(require_api_key)],
)
app.include_router(
    suppressions.router,
    prefix="/api",
    dependencies=[Depends(require_api_key)],
)
app.include_router(
    campaigns.router,
    prefix="/api",
    dependencies=[Depends(require_api_key)],
)
app.include_router(
    templates.router,
    prefix="/api",
    dependencies=[Depends(require_api_key)],
)
app.include_router(
    workflows.router,
    prefix="/api",
    dependencies=[Depends(require_api_key)],
)
app.include_router(
    stats.router,
    prefix="/api",
)
app.include_router(
    recipients.router,
    prefix="/api",
    dependencies=[Depends(require_api_key)],
)
app.include_router(
    alerts.router,
    prefix="/api",
)
