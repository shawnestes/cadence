from datetime import datetime

from pydantic import BaseModel


class Topline(BaseModel):
    sends: int
    delivered: int
    bounces: int
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
