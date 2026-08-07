"""Frozen types describing a tenant's record shape.

Lives in the domain layer so app/domain/record_mapping.py can consume it
without importing the service layer. No FastAPI, no SQLAlchemy, no YAML —
parsing is app/services/tenant_mapping.py's job.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    source: str
    normalize: str | None = None
    required: bool = True


@dataclass(frozen=True)
class RepeatingSpec:
    name: str
    column_template: str
    json_key: str
    fields: tuple[str, ...]
    max: int


@dataclass(frozen=True)
class DerivedSpec:
    name: str
    func: str          # "count" | "list" | "max"
    source: str        # repeating-group name
    field: str | None  # None for count()


@dataclass(frozen=True)
class ConsentSpec:
    channels: tuple[int, ...]
    required: bool
    source_label: str
    text: FieldSpec
    captured_at: FieldSpec
    optional: Mapping[str, FieldSpec]


@dataclass(frozen=True)
class TenantMapping:
    tenant: str
    email_source: str
    external_id_source: str | None
    vertical_source: str | None
    fields: Mapping[str, FieldSpec]
    repeating: tuple[RepeatingSpec, ...]
    derived: tuple[DerivedSpec, ...]
    consent: ConsentSpec
