from fastapi import Depends, FastAPI

from app.api import (
    campaigns,
    health,
    ingest,
    metrics,
    suppressions,
    templates,
    unsub,
    webhooks,
    workflows,
)
from app.core.auth import require_api_key
from app.core.config import get_settings
from app.core.logging import configure_logging

configure_logging(get_settings().log_level)

app = FastAPI(title=get_settings().app_name)
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
