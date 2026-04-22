from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


TemplateStatus = Literal["generated", "approved", "rejected", "retired"]


class TemplateIn(BaseModel):
    campaign_id: UUID
    tenant: str = "otto"
    cohort_key: str
    variant_label: str
    subject: str
    preheader: str | None = None
    body_html: str
    body_text: str
    ai_meta: dict | None = None


class TemplateUpdate(BaseModel):
    status: TemplateStatus | None = None
    subject: str | None = None
    preheader: str | None = None
    body_html: str | None = None
    body_text: str | None = None


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant: str
    campaign_id: UUID
    cohort_key: str
    variant_label: str
    subject: str
    preheader: str | None
    body_html: str
    body_text: str
    ai_meta: dict | None
    status: str
    created_at: datetime
    updated_at: datetime


class TemplateListOut(BaseModel):
    templates: list[TemplateOut]
    total: int
