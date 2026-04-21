from fastapi import FastAPI

from app.api import health, metrics
from app.core.config import get_settings
from app.core.logging import configure_logging

configure_logging(get_settings().log_level)

app = FastAPI(title=get_settings().app_name)
app.include_router(health.router)
app.include_router(metrics.router)
