from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


CampaignStatus = Literal["draft", "generating", "ready", "active", "paused", "archived"]


class CampaignIn(BaseModel):
    name: str
    tenant: str = "otto"
    vertical: str
    workflow_id: UUID | None = None
    node_id: str | None = None
    sending_identity_id: UUID | None = None
    cohort_filter: dict | None = None
    ai_brief: str | None = None
    mab_config: dict = Field(default_factory=lambda: {"algo": "thompson", "explore_pct": 0.1})


class CampaignUpdate(BaseModel):
    name: str | None = None
    status: CampaignStatus | None = None
    sending_identity_id: UUID | None = None
    cohort_filter: dict | None = None
    ai_brief: str | None = None
    mab_config: dict | None = None


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant: str
    workflow_id: UUID | None
    node_id: str | None
    name: str
    vertical: str
    status: str
    sending_identity_id: UUID | None
    cohort_filter: dict | None
    ai_brief: str | None
    mab_config: dict
    created_at: datetime
    updated_at: datetime


class CampaignListOut(BaseModel):
    campaigns: list[CampaignOut]
    total: int


class CampaignSendRequest(BaseModel):
    recipient_ids: list[UUID] | None = None
    email_list: list[str] | None = None
    segment: dict | None = None
    tenant: str = "otto"


class CampaignSendResult(BaseModel):
    queued: int
    skipped_no_template: int
    skipped_not_found: int
