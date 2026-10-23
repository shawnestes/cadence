from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_api_key
from app.core.config import get_settings
from app.core.db import get_db
from app.models.postmaster_daily import PostmasterDaily
from app.workers.postmaster_sync import METRIC as GMAIL_SPAM_METRIC

router = APIRouter(tags=["postmaster"])

# Gmail reports a ratio; surface the percentage too so a caller cannot
# misread 0.003 as 0.3 and draw the wrong conclusion about the ceiling.
_PCT = 100.0


class PostmasterPoint(BaseModel):
    day: date
    metric: str
    value: float
    value_pct: float
    fetched_at: datetime


class PostmasterHistoryOut(BaseModel):
    domain: str
    metric: str
    points: list[PostmasterPoint]
    warn_threshold: float
    critical_threshold: float
    # Thresholds travel with the series so a chart can draw the lines that
    # actually govern deliverability rather than hard-coding them.


@router.get(
    "/postmaster/history",
    response_model=PostmasterHistoryOut,
    dependencies=[Depends(require_api_key)],
)
async def postmaster_history(
    metric: str = Query(
        "SPAM_RATE",
        description="Postmaster metric name, e.g. SPAM_RATE, DELIVERY_ERROR_RATE",
    ),
    days: int = Query(90, ge=1, le=365),
    domain: str | None = Query(
        None, description="Defaults to the configured Postmaster domain"
    ),
    db: AsyncSession = Depends(get_db),
) -> PostmasterHistoryOut:
    """Daily Postmaster readings, oldest first.

    Days with no row are genuinely absent, not zero: Postmaster is
    volume-gated and reports nothing below a daily threshold. Gaps are left as
    gaps rather than filled, because a filled zero would read as a clean day.
    """
    settings = get_settings()
    target = domain or settings.postmaster_domain or ""
    if target and not target.startswith("domains/"):
        target = f"domains/{target}"

    since = date.today() - timedelta(days=days)
    rows = (await db.execute(
        select(PostmasterDaily)
        .where(
            PostmasterDaily.domain == target,
            PostmasterDaily.metric == metric,
            PostmasterDaily.day >= since,
        )
        .order_by(PostmasterDaily.day.asc())
    )).scalars().all()

    return PostmasterHistoryOut(
        domain=target,
        metric=metric,
        points=[
            PostmasterPoint(
                day=r.day,
                metric=r.metric,
                value=float(r.value),
                value_pct=float(r.value) * _PCT,
                fetched_at=r.fetched_at,
            )
            for r in rows
        ],
        warn_threshold=settings.postmaster_spam_rate_warn,
        critical_threshold=settings.postmaster_spam_rate_critical,
    )
