import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import SmallInteger

from app.models.base import Base, uuid7


class ConsentProof(Base):
    __tablename__ = "consent_proof"

    consent_proof_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    tenant: Mapped[str] = mapped_column(Text, nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recipients.id", name="fk_consent_proof_contact_id"),
        nullable=False,
    )
    captured_at: Mapped[datetime] = mapped_column(
        "captured_at", nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    form_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    disclosure_text_hash: Mapped[str] = mapped_column(Text, nullable=False)
    channels: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger()), nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_consent_proof_tenant_contact", "tenant", "contact_id"),
    )
