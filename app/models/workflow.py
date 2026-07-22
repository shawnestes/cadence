import uuid

from sqlalchemy import Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid7


class Workflow(Base, TimestampMixin):
    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    vertical: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    entry_segment: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ISO-8601 duration (e.g. "P30D") a recipient must wait after their prior
    # journey's terminal timestamp before re-entering. NULL = never re-enter
    # (SPEC §6.5 default). "PT0S" = re-entry allowed immediately.
    reentry_cooldown: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_workflows_tenant_status", "tenant", "status"),
    )
