from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_api_key
from app.core.db import get_db
from app.models.alert import Alert

router = APIRouter(tags=["alerts"])


class AlertOut(BaseModel):
    id: str
    tenant: str
    metric: str
    severity: str
    threshold: float
    value: float
    status: str
    pending_since: datetime
    activated_at: datetime | None
    resolved_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class AlertsListOut(BaseModel):
    alerts: list[AlertOut]
    total: int


def _to_out(a: Alert) -> AlertOut:
    return AlertOut(
        id=str(a.id),
        tenant=a.tenant,
        metric=a.metric,
        severity=a.severity,
        threshold=a.threshold,
        value=a.value,
        status=a.status,
        pending_since=a.pending_since,
        activated_at=a.activated_at,
        resolved_at=a.resolved_at,
        notes=a.notes,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


@router.get(
    "/alerts",
    response_model=AlertsListOut,
    dependencies=[Depends(require_api_key)],
)
async def list_alerts(
    tenant: str = Query(default="otto"),
    status: str | None = Query(default=None, description="Filter by status. Pass 'all' to include all statuses. Default returns pending+active."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> AlertsListOut:
    q = select(Alert).where(Alert.tenant == tenant)

    if status is None:
        # Default: non-resolved only
        q = q.where(Alert.status.in_(("pending", "active")))
    elif status == "all":
        pass  # No status filter
    else:
        q = q.where(Alert.status == status)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    q = q.order_by(Alert.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(q)).scalars().all()

    return AlertsListOut(alerts=[_to_out(a) for a in rows], total=total)
