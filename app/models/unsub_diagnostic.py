import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7


class UnsubDiagnostic(Base):
    """A failed unsubscribe lookup (e.g. valid token, no matching recipient).

    Users always get a friendly page; the anomaly lands here for diagnosis
    instead of surfacing as a 404.
    """

    __tablename__ = "unsub_diagnostics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    token: Mapped[str | None] = mapped_column(Text, nullable=True)
    host: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_unsub_diagnostics_created_at", "created_at"),)
