import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import SmallInteger

from app.models.base import Base, uuid7


class ContactEvent(Base):
    __tablename__ = "contact_event"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    occurred_at: Mapped[datetime] = mapped_column(primary_key=True, nullable=False)
    tenant: Mapped[str] = mapped_column(Text, nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recipients.id"),
        nullable=False,
    )
    channel: Mapped[int] = mapped_column(SmallInteger(), nullable=False)
    event_type: Mapped[int] = mapped_column(SmallInteger(), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id"),
        nullable=True,
    )
    source: Mapped[int] = mapped_column(SmallInteger(), nullable=False)
    provider_msg_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    click_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedup_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    classification: Mapped[int | None] = mapped_column(SmallInteger(), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = {"postgresql_partition_by": "RANGE (occurred_at)"}
