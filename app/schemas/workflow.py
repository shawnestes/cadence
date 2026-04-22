from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.workflow_defs import WorkflowDefinition


WorkflowStatus = Literal["draft", "active", "paused", "archived"]


class WorkflowIn(BaseModel):
    name: str
    tenant: str = "otto"
    vertical: str
    definition: WorkflowDefinition
    entry_segment: dict | None = None
    created_by: str | None = None


class WorkflowUpdate(BaseModel):
    name: str | None = None
    status: WorkflowStatus | None = None
    definition: WorkflowDefinition | None = None
    entry_segment: dict | None = None


class WorkflowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant: str
    name: str
    vertical: str
    status: str
    definition: dict
    entry_segment: dict | None
    version: int
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class WorkflowListOut(BaseModel):
    workflows: list[WorkflowOut]
    total: int
