import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import SmallInteger

from app.models.base import Base


class ClickRegistry(Base):
    __tablename__ = "click_registry"

    click_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant: Mapped[str] = mapped_column(Text, nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recipients.id", name="fk_click_registry_contact_id"),
        nullable=False,
    )
    channel: Mapped[int] = mapped_column(SmallInteger(), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    clicked_at: Mapped[datetime] = mapped_column(nullable=False)
    classification: Mapped[int] = mapped_column(
        SmallInteger(), nullable=False, server_default=text("0")
    )
    reclassified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    convert_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=text("0")
    )

    __table_args__ = (
        Index("ix_click_registry_contact_clicked", "contact_id", "clicked_at"),
    )
