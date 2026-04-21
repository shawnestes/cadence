import uuid
from datetime import datetime

import uuid_utils
from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType


def uuid7() -> uuid.UUID:
    # uuid_utils returns its own UUID type; convert to stdlib uuid.UUID for SQLAlchemy.
    return uuid.UUID(bytes=uuid_utils.uuid7().bytes)


class Citext(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "CITEXT"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
