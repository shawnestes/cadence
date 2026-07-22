from pydantic import BaseModel, Field


class SegmentPreviewRequest(BaseModel):
    segment: dict | None = None
    tenant: str = "otto"


class SegmentPreviewRecipient(BaseModel):
    id: str
    email: str
    vertical: str
    engagement_tier: str


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
