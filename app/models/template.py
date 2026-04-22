import uuid

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid7


class Template(Base, TimestampMixin):
    __tablename__ = "templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant: Mapped[str] = mapped_column(Text, nullable=False)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    cohort_key: Mapped[str] = mapped_column(Text, nullable=False)
    variant_label: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    preheader: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    ai_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'generated'"))

    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "cohort_key", "variant_label", name="uq_templates_campaign_cohort_variant"
        ),
        Index("ix_templates_campaign_cohort_status", "campaign_id", "cohort_key", "status"),
    )
