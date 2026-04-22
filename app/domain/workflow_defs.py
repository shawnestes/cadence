from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EntryNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["entry"]
    next: str


class SendNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["send"]
    campaign_id: str
    next: str


class WaitNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["wait"]
    duration: str
    next: str


class EventInWindowCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["event_in_window"]
    event_type: Literal["open", "click", "any_engagement"]
    since_node: str
    window: str


class ClickUrlMatchCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["click_url_match"]
    since_node: str
    url_contains: str
    window: str


class CompositeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["and", "or"]
    operands: list["BranchCondition"]


BranchCondition = Annotated[
    Union[EventInWindowCondition, ClickUrlMatchCondition, CompositeCondition],
    Field(discriminator="kind"),
]
CompositeCondition.model_rebuild()


class BranchNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["branch"]
    condition: BranchCondition
    if_true: str
    if_false: str


class ExitNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["exit"]
    reason: str


WorkflowNode = Annotated[
    Union[EntryNode, SendNode, WaitNode, BranchNode, ExitNode],
    Field(discriminator="type"),
]


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    name: str
    vertical: str
    entry: str
    nodes: dict[str, WorkflowNode]

    @field_validator("nodes")
    @classmethod
    def _validate_nodes(cls, v):
        if not v:
            raise ValueError("workflow must have at least one node")
        return v

    def validate_graph(self) -> list[str]:
        errors: list[str] = []
        if self.entry not in self.nodes:
            errors.append(f"entry node '{self.entry}' not found")
            return errors
        for node_id, node in self.nodes.items():
            for ref_attr in ("next", "if_true", "if_false"):
                ref = getattr(node, ref_attr, None)
                if ref is not None and ref not in self.nodes:
                    errors.append(f"node '{node_id}' references missing node '{ref}'")
        visited: set[str] = set()
        path: set[str] = set()

        def dfs(nid: str) -> None:
            if nid in path:
                errors.append(f"cycle detected involving node '{nid}'")
                return
            if nid in visited:
                return
            visited.add(nid)
            path.add(nid)
            n = self.nodes.get(nid)
            if n is not None:
                for ref_attr in ("next", "if_true", "if_false"):
                    ref = getattr(n, ref_attr, None)
                    if ref is not None and ref in self.nodes:
                        dfs(ref)
            path.discard(nid)

        dfs(self.entry)
        return errors


def parse_iso_duration(duration: str) -> timedelta:
    m = re.fullmatch(
        r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
        duration,
    )
    if not m or not any(m.groups()):
        raise ValueError(f"invalid ISO 8601 duration: {duration}")
    weeks, days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
