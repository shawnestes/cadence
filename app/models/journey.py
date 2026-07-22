import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7


class Journey(Base):
    __tablename__ = "journeys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id"), nullable=False
    )
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipients.id"), nullable=False
    )
    current_node_id: Mapped[str] = mapped_column(Text, nullable=False)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Partial unique: one in-flight journey per (workflow, recipient).
        # Terminal statuses (completed/exited/errored) are excluded so a
        # recipient can re-enter after completion per SPEC §6.5.
        Index(
            "uq_journeys_active_workflow_recipient",
            "workflow_id",
            "recipient_id",
            unique=True,
            postgresql_where=text("status IN ('active','scheduled','waiting')"),
        ),
        Index(
            "ix_journeys_active_next_action",
            "status",
            "next_action_at",
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_journeys_recipient_id", "recipient_id"),
    )
