from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RecipientSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    tenant: str
    vertical: str
    status: str
    external_id: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime
    attributes: dict


class JourneySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workflow_id: UUID
    workflow_version: int
    status: str
    current_node_id: str
    entered_at: datetime
    completed_at: datetime | None


class SendSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    template_id: UUID
    variant_label: str
    status: str
    queued_at: datetime
    sent_at: datetime | None
    delivered_at: datetime | None
    failure_reason: str | None


class EventSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    event_subtype: str | None
    url: str | None
    occurred_at: datetime


class RecipientDetail(BaseModel):
    recipient: RecipientSummary
    journeys: list[JourneySummary]
    sends: list[SendSummary]
    events: list[EventSummary]


class RecipientSearchOut(BaseModel):
    recipients: list[RecipientSummary]
    total: int
