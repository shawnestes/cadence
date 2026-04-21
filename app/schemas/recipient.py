from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RecipientIngestIn(BaseModel):
    email: EmailStr
    tenant: str = "otto"
    external_id: str | None = None
    vertical: Literal["auto", "home"]
    consent_source: str
    consent_at: datetime
    attributes: dict = Field(default_factory=dict)


class RecipientIngestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant: str
    email: str
    status: str
    created: bool
