import uuid

from sqlalchemy import Boolean, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid7


class SendingIdentity(Base, TimestampMixin):
    __tablename__ = "sending_identities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    esp_provider: Mapped[str] = mapped_column(Text, nullable=False)
    from_address: Mapped[str] = mapped_column(Text, nullable=False)
    sending_domain: Mapped[str] = mapped_column(Text, nullable=False)
    credentials_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    daily_quota: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("50000")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    __table_args__ = (
        UniqueConstraint("tenant", "name", name="uq_sending_identities_tenant_name"),
    )
