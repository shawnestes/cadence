from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr


class SuppressionIn(BaseModel):
    email: EmailStr
    tenant: str = "otto"
    reason: Literal["unsubscribe", "hard_bounce", "complaint", "manual", "otto_push"]
    source: str
    campaign_id: UUID | None = None
    notes: str | None = None


class SuppressionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant: str
    email: str
    reason: str
    source: str
    campaign_id: UUID | None = None
    notes: str | None = None
    suppressed_at: datetime
    created_at: datetime
    updated_at: datetime


class SuppressionListOut(BaseModel):
    suppressions: list[SuppressionOut]
    total: int
