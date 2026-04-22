from datetime import datetime

from pydantic import BaseModel


class Topline(BaseModel):
    sends: int
    delivered: int
    bounces: int
    hard_bounces: int
    soft_bounces: int
    complaints: int
    unique_opens: int
    unique_clicks: int
    unsubscribes: int
    delivery_rate: float
    bounce_rate: float
    complaint_rate: float
    open_rate: float
    click_rate: float
    unsubscribe_rate: float


class ToplineWindow(BaseModel):
    window: str  # "24h" | "7d" | "30d"
    topline: Topline


class TimeseriesPoint(BaseModel):
    bucket_start: datetime
    sends: int
    delivered: int
    bounces: int
    complaints: int
    opens: int
    clicks: int


class TimeseriesOut(BaseModel):
    points: list[TimeseriesPoint]
    bucket: str  # "1h" | "1d"


class CampaignVolumeRow(BaseModel):
    campaign_id: str
    campaign_name: str
    sends: int
    delivered: int
    bounces: int
    complaints: int
    delivery_rate: float
    bounce_rate: float
    complaint_rate: float


class CampaignsVolumeOut(BaseModel):
    window: str
    campaigns: list[CampaignVolumeRow]


class SESReputationOut(BaseModel):
    tenant: str
    sending_enabled: bool
    production_access_enabled: bool
    enforcement_status: str | None
    max_24_hour_send: float
    sent_last_24_hours: float
    max_send_rate: float
    reputation_sending_enabled: bool | None
    fetched_at: datetime
