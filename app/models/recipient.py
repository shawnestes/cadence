import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Citext, TimestampMixin, uuid7


class Recipient(Base, TimestampMixin):
    __tablename__ = "recipients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Citext, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    vertical: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consent_source: Mapped[str] = mapped_column(Text, nullable=False)
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attributes: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    engagement_tier: Mapped[str] = mapped_column(
        Text, nullable=False, default="neutral", server_default=text("'neutral'")
    )
    tier_computed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("tenant", "email", name="uq_recipients_tenant_email"),
        Index("ix_recipients_status_vertical", "status", "vertical"),
        Index("ix_recipients_attributes_gin", "attributes", postgresql_using="gin"),
    )
