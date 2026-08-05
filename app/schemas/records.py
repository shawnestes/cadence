from typing import Any, Literal

from pydantic import BaseModel, Field


class RecordBatchIn(BaseModel):
    tenant: str = "otto"
    vertical: Literal["auto", "home"]
    enroll: bool = False
    # Three DB writes per record: 200 keeps the request inside gateway timeouts.
    records: list[dict[str, Any]] = Field(max_length=200)


class RecordResult(BaseModel):
    index: int
    status: Literal["created", "updated", "quarantined", "rejected"]
    errors: list[str] = []


class RecordBatchOut(BaseModel):
    results: list[RecordResult]
    counters: dict[str, int]
