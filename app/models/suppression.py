import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Citext, TimestampMixin, uuid7


class Suppression(Base, TimestampMixin):
    __tablename__ = "suppressions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Citext, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    suppressed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant", "email", name="uq_suppressions_tenant_email"),
        Index("ix_suppressions_email_hash", "email", postgresql_using="hash"),
    )
