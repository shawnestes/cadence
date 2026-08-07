import uuid

from sqlalchemy import ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid7


class Campaign(Base, TimestampMixin):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    vertical: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    sending_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sending_identities.id"), nullable=True
    )
    cohort_filter: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    segment_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_brief: Mapped[str | None] = mapped_column(Text, nullable=True)
    mab_config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {"algo": "thompson", "explore_pct": 0.1},
        server_default=text(r"""'{"algo":"thompson","explore_pct"\:0.1}'::jsonb"""),
    )

    __table_args__ = (
        Index("ix_campaigns_tenant_status", "tenant", "status"),
    )
