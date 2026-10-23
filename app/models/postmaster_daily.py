from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Index, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PostmasterDaily(Base):
    """One Google Postmaster metric reading for one domain on one day.

    Natural primary key rather than a surrogate id: Postmaster restates recent
    days as more data arrives, and the sync re-reads a lookback window on every
    run, so (domain, day, metric) must collapse on conflict instead of
    accumulating duplicate readings of the same day.

    Numeric, not Float: these are ratios evaluated against 0.001 and 0.003, and
    binary float drift at a threshold that governs sending reputation is not a
    trade worth making.
    """

    __tablename__ = "postmaster_daily"

    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    metric: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_postmaster_daily_metric_day", "domain", "metric", "day"),
    )
