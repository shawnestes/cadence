from pydantic import BaseModel, Field


class SegmentPreviewRequest(BaseModel):
    segment: dict | None = None
    tenant: str = "otto"
    acknowledges_cold: bool = False
    include_unsendable: bool = False


class SegmentPreviewRecipient(BaseModel):
    contact_id: str
    email_domain: str | None
    mailbox_provider: int | None
    engagement_tier: int | None
    engagement_score: int | None


class SegmentPreviewResponse(BaseModel):
    count: int
    sample: list[SegmentPreviewRecipient]


class WorkflowBackfillRequest(BaseModel):
    segment: dict | None = None
    limit: int = Field(le=10000, gt=0)


class WorkflowBackfillResponse(BaseModel):
    enrolled: int
    skipped_active: int
    skipped_cooldown: int
    skipped_suppressed: int
