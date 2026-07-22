from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.domain.duration import InvalidDurationError, parse_iso8601_duration
from app.domain.workflow_defs import WorkflowDefinition


WorkflowStatus = Literal["draft", "active", "paused", "archived"]


def _validate_reentry_cooldown(value: str | None) -> str | None:
    """Validate ``reentry_cooldown`` parses as an ISO-8601 duration.

    ``None`` (never re-enter, the SPEC §6.5 default) passes through
    unchanged.
    """
    if value is None:
        return value
    try:
        parse_iso8601_duration(value)
    except InvalidDurationError as exc:
        raise ValueError(str(exc)) from exc
    return value


class WorkflowIn(BaseModel):
    name: str
    tenant: str = "otto"
    vertical: str
    definition: WorkflowDefinition
    entry_segment: dict | None = None
    created_by: str | None = None
    reentry_cooldown: str | None = None

    _validate_reentry_cooldown = field_validator("reentry_cooldown")(
        _validate_reentry_cooldown
    )


class WorkflowUpdate(BaseModel):
    name: str | None = None
    status: WorkflowStatus | None = None
    definition: WorkflowDefinition | None = None
    entry_segment: dict | None = None
    reentry_cooldown: str | None = None

    _validate_reentry_cooldown = field_validator("reentry_cooldown")(
        _validate_reentry_cooldown
    )


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
    reentry_cooldown: str | None
    created_at: datetime
    updated_at: datetime


class WorkflowListOut(BaseModel):
    workflows: list[WorkflowOut]
    total: int
