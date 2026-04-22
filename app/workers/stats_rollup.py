import structlog
from sqlalchemy import text

from app.core.db import engine

log = structlog.get_logger(__name__)


async def run_once() -> dict:
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY variant_stats"))
    log.info("stats_rollup.refreshed")
    return {"refreshed": True}
