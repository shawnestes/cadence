import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

import boto3
import structlog
from botocore.exceptions import ClientError, NoCredentialsError
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_api_key
from app.core.config import get_settings
from app.core.db import async_session_factory, get_db
from app.models.campaign import Campaign
from app.models.event import Event
from app.models.send import Send
from app.schemas.stats import (
    CampaignVolumeRow,
    CampaignsVolumeOut,
    SESReputationOut,
    Topline,
    TimeseriesOut,
    TimeseriesPoint,
    ToplineWindow,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["stats"])


WindowLiteral = Literal["24h", "7d", "30d"]
BucketLiteral = Literal["1h", "1d"]


def _window_start(window: str) -> datetime:
    now = datetime.now(timezone.utc)
    if window == "24h":
        return now - timedelta(hours=24)
    if window == "7d":
        return now - timedelta(days=7)
    if window == "30d":
        return now - timedelta(days=30)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="window must be one of 24h, 7d, 30d",
    )


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


async def _compute_topline(db: AsyncSession, tenant: str, window: str) -> Topline:
    start = _window_start(window)

    sends_q = select(
        func.count().filter(
            Send.status.in_(("sent", "delivered", "bounced", "complained"))
        ).label("sends"),
        func.count().filter(Send.status == "delivered").label("delivered"),
        func.count().filter(Send.status == "bounced").label("bounces"),
        func.count().filter(Send.status == "complained").label("complaints"),
        func.count().filter(Send.failure_reason == "hard_bounce").label("hard_bounces"),
        func.count().filter(Send.failure_reason == "soft_bounce").label("soft_bounces"),
    ).where(Send.tenant == tenant, Send.queued_at >= start)
    sends_row = (await db.execute(sends_q)).one()

    events_q = select(
        func.count(func.distinct(Event.recipient_id))
        .filter(Event.event_type == "open")
        .label("unique_opens"),
        func.count(func.distinct(Event.recipient_id))
        .filter(Event.event_type == "click")
        .label("unique_clicks"),
        func.count().filter(Event.event_type == "unsubscribe").label("unsubscribes"),
    ).where(Event.tenant == tenant, Event.occurred_at >= start)
    events_row = (await db.execute(events_q)).one()

    sends_count = int(sends_row.sends or 0)
    delivered = int(sends_row.delivered or 0)
    bounces = int(sends_row.bounces or 0)
    hard_bounces = int(sends_row.hard_bounces or 0)
    soft_bounces = int(sends_row.soft_bounces or 0)
    complaints = int(sends_row.complaints or 0)
    unique_opens = int(events_row.unique_opens or 0)
    unique_clicks = int(events_row.unique_clicks or 0)
    unsubscribes = int(events_row.unsubscribes or 0)

    return Topline(
        sends=sends_count,
        delivered=delivered,
        bounces=bounces,
        hard_bounces=hard_bounces,
        soft_bounces=soft_bounces,
        complaints=complaints,
        unique_opens=unique_opens,
        unique_clicks=unique_clicks,
        unsubscribes=unsubscribes,
        delivery_rate=_rate(delivered, sends_count),
        bounce_rate=_rate(bounces, sends_count),
        complaint_rate=_rate(complaints, sends_count),
        open_rate=_rate(unique_opens, delivered),
        click_rate=_rate(unique_clicks, delivered),
        unsubscribe_rate=_rate(unsubscribes, delivered),
    )


@router.get(
    "/stats/topline",
    response_model=ToplineWindow,
    dependencies=[Depends(require_api_key)],
)
async def get_topline(
    tenant: str = Query(default="otto"),
    window: WindowLiteral = Query(default="24h"),
    db: AsyncSession = Depends(get_db),
) -> ToplineWindow:
    topline = await _compute_topline(db, tenant, window)
    return ToplineWindow(window=window, topline=topline)


@router.get(
    "/stats/timeseries",
    response_model=TimeseriesOut,
    dependencies=[Depends(require_api_key)],
)
async def get_timeseries(
    tenant: str = Query(default="otto"),
    window: WindowLiteral = Query(default="24h"),
    bucket: BucketLiteral = Query(default="1h"),
    db: AsyncSession = Depends(get_db),
) -> TimeseriesOut:
    start = _window_start(window)
    trunc = "hour" if bucket == "1h" else "day"

    s_bucket = func.date_trunc(trunc, Send.queued_at).label("bucket_start")
    sends_q = (
        select(
            s_bucket,
            func.count()
            .filter(Send.status.in_(("sent", "delivered", "bounced", "complained")))
            .label("sends"),
            func.count().filter(Send.status == "delivered").label("delivered"),
            func.count().filter(Send.status == "bounced").label("bounces"),
            func.count().filter(Send.status == "complained").label("complaints"),
        )
        .where(Send.tenant == tenant, Send.queued_at >= start)
        .group_by(s_bucket)
    )
    sends_rows = (await db.execute(sends_q)).all()

    e_bucket = func.date_trunc(trunc, Event.occurred_at).label("bucket_start")
    events_q = (
        select(
            e_bucket,
            func.count().filter(Event.event_type == "open").label("opens"),
            func.count().filter(Event.event_type == "click").label("clicks"),
        )
        .where(Event.tenant == tenant, Event.occurred_at >= start)
        .group_by(e_bucket)
    )
    events_rows = (await db.execute(events_q)).all()

    points: dict[datetime, dict] = {}
    for r in sends_rows:
        points[r.bucket_start] = {
            "bucket_start": r.bucket_start,
            "sends": int(r.sends or 0),
            "delivered": int(r.delivered or 0),
            "bounces": int(r.bounces or 0),
            "complaints": int(r.complaints or 0),
            "opens": 0,
            "clicks": 0,
        }
    for r in events_rows:
        p = points.setdefault(
            r.bucket_start,
            {
                "bucket_start": r.bucket_start,
                "sends": 0,
                "delivered": 0,
                "bounces": 0,
                "complaints": 0,
                "opens": 0,
                "clicks": 0,
            },
        )
        p["opens"] = int(r.opens or 0)
        p["clicks"] = int(r.clicks or 0)

    ordered = [TimeseriesPoint(**points[k]) for k in sorted(points.keys())]
    return TimeseriesOut(points=ordered, bucket=bucket)


def _require_api_key_or_query(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    api_key: str | None = Query(default=None),
) -> None:
    expected = get_settings().api_key
    presented = x_api_key or api_key
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
        )


@router.get("/stats/stream")
async def stream_topline(
    tenant: str = Query(default="otto"),
    window: WindowLiteral = Query(default="24h"),
    _: None = Depends(_require_api_key_or_query),
) -> StreamingResponse:
    async def event_generator():
        try:
            while True:
                async with async_session_factory() as db:
                    topline = await _compute_topline(db, tenant, window)
                payload = {
                    "type": "topline",
                    "data": json.loads(topline.model_dump_json()),
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "window": window,
                }
                yield f"data: {json.dumps(payload)}\n\n"
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get(
    "/stats/campaigns-volume",
    response_model=CampaignsVolumeOut,
    dependencies=[Depends(require_api_key)],
)
async def get_campaigns_volume(
    tenant: str = Query(default="otto"),
    window: WindowLiteral = Query(default="24h"),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> CampaignsVolumeOut:
    start = _window_start(window)

    q = (
        select(
            Send.campaign_id.label("campaign_id"),
            Campaign.name.label("campaign_name"),
            func.count().filter(
                Send.status.in_(("sent", "delivered", "bounced", "complained"))
            ).label("sends"),
            func.count().filter(Send.status == "delivered").label("delivered"),
            func.count().filter(Send.status == "bounced").label("bounces"),
            func.count().filter(Send.status == "complained").label("complaints"),
        )
        .join(Campaign, Campaign.id == Send.campaign_id)
        .where(Send.tenant == tenant, Send.queued_at >= start)
        .group_by(Send.campaign_id, Campaign.name)
        .order_by(func.count().filter(
            Send.status.in_(("sent", "delivered", "bounced", "complained"))
        ).desc())
        .limit(limit)
    )
    rows = (await db.execute(q)).all()

    campaigns = [
        CampaignVolumeRow(
            campaign_id=str(r.campaign_id),
            campaign_name=r.campaign_name,
            sends=int(r.sends or 0),
            delivered=int(r.delivered or 0),
            bounces=int(r.bounces or 0),
            complaints=int(r.complaints or 0),
            delivery_rate=_rate(int(r.delivered or 0), int(r.sends or 0)),
            bounce_rate=_rate(int(r.bounces or 0), int(r.sends or 0)),
            complaint_rate=_rate(int(r.complaints or 0), int(r.sends or 0)),
        )
        for r in rows
    ]

    return CampaignsVolumeOut(window=window, campaigns=campaigns)


@router.get(
    "/stats/ses-reputation",
    response_model=SESReputationOut,
    dependencies=[Depends(require_api_key)],
)
async def get_ses_reputation(
    tenant: str = Query(default="otto"),
) -> SESReputationOut:
    settings = get_settings()

    def _fetch_account():
        client = boto3.client("sesv2", region_name=settings.aws_region)
        return client.get_account()

    try:
        account = await asyncio.to_thread(_fetch_account)
    except NoCredentialsError as exc:
        logger.warning("ses_reputation_no_credentials", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SES credentials not configured",
        ) from exc
    except ClientError as exc:
        logger.warning("ses_reputation_client_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"SES unavailable: {exc.response['Error']['Message']}",
        ) from exc
    except Exception as exc:
        logger.warning("ses_reputation_unexpected_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"SES unavailable: {exc}",
        ) from exc

    quota = account.get("SendQuota") or {}
    details = account.get("Details") or {}
    review = details.get("ReviewDetails") or {}

    reputation_sending_enabled: bool | None = None
    if review:
        val = review.get("SendingEnabled")
        if val is not None:
            reputation_sending_enabled = bool(val)

    return SESReputationOut(
        tenant=tenant,
        sending_enabled=bool(account.get("SendingEnabled", False)),
        production_access_enabled=bool(account.get("ProductionAccessEnabled", False)),
        enforcement_status=account.get("EnforcementStatus") or None,
        max_24_hour_send=float(quota.get("Max24HourSend") or 0.0),
        sent_last_24_hours=float(quota.get("SentLast24Hours") or 0.0),
        max_send_rate=float(quota.get("MaxSendRate") or 0.0),
        reputation_sending_enabled=reputation_sending_enabled,
        fetched_at=datetime.now(timezone.utc),
    )
