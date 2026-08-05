"""Per-tenant record mapping loaded from config/tenants/<tenant>.yaml.

Mirrors app/services/segment_loader.py: YAML on disk, validated into frozen
dataclasses, cached per process. A new tenant is a new YAML file, not code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from app.domain.normalizers import NORMALIZERS
from app.domain.tenant_spec import (
    ConsentSpec,
    DerivedSpec,
    FieldSpec,
    RepeatingSpec,
    TenantMapping,
)

# app/services/tenant_mapping.py -> app/services -> app -> repo root
_REPO_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_MAPPINGS_DIR = _REPO_ROOT / "config" / "tenants"

_DERIVED_RE = re.compile(r"^(count|list|max)\(([a-z0-9_]+)(?:\.([a-z0-9_]+))?\)$")

_CONSENT_OPTIONAL_KEYS = ("source_url", "form_id", "ip", "user_agent")

_cache: dict[tuple[str, str], TenantMapping] = {}


def _field_spec(raw: Any, where: str) -> FieldSpec:
    if isinstance(raw, str):
        return FieldSpec(source=raw)
    if not isinstance(raw, dict) or "from" not in raw:
        raise ValueError(f"{where}: expected a string or a mapping with a 'from' key")
    normalize = raw.get("normalize")
    if normalize is not None and normalize not in NORMALIZERS:
        raise ValueError(
            f"{where}: unknown normalizer {normalize!r}; "
            f"known: {sorted(NORMALIZERS)}"
        )
    return FieldSpec(
        source=raw["from"],
        normalize=normalize,
        required=bool(raw.get("required", True)),
    )


def _parse_consent(raw: Any) -> ConsentSpec:
    if not isinstance(raw, dict):
        raise ValueError("consent: block is required and must be a mapping")
    for key in ("text", "captured_at", "source_label"):
        if key not in raw:
            raise ValueError(f"consent: missing required key {key!r}")
    channels = tuple(int(c) for c in raw.get("channels", [1]))
    if not channels:
        raise ValueError("consent: channels must not be empty")
    optional = {
        key: _field_spec(raw[key], f"consent.{key}")
        for key in _CONSENT_OPTIONAL_KEYS
        if key in raw
    }
    return ConsentSpec(
        channels=channels,
        required=bool(raw.get("required", True)),
        source_label=str(raw["source_label"]),
        text=_field_spec(raw["text"], "consent.text"),
        captured_at=_field_spec(raw["captured_at"], "consent.captured_at"),
        optional=optional,
    )


def _parse_repeating(raw: Any) -> tuple[RepeatingSpec, ...]:
    if not raw:
        return ()
    specs: list[RepeatingSpec] = []
    for name, body in raw.items():
        if "columns" not in body or "fields" not in body:
            raise ValueError(f"repeating.{name}: 'columns' and 'fields' are required")
        specs.append(
            RepeatingSpec(
                name=name,
                column_template=body["columns"],
                json_key=body.get("json_key", name),
                fields=tuple(body["fields"]),
                max=int(body.get("max", 5)),
            )
        )
    return tuple(specs)


def _parse_derived(raw: Any, repeating: tuple[RepeatingSpec, ...]) -> tuple[DerivedSpec, ...]:
    if not raw:
        return ()
    known = {r.name for r in repeating}
    specs: list[DerivedSpec] = []
    for name, expr in raw.items():
        m = _DERIVED_RE.match(str(expr).strip())
        if m is None:
            raise ValueError(
                f"derived.{name}: expected count(<group>), list(<group>.<field>) "
                f"or max(<group>.<field>), got {expr!r}"
            )
        func, source, field = m.group(1), m.group(2), m.group(3)
        if source not in known:
            raise ValueError(f"derived.{name}: unknown repeating group {source!r}")
        if func in ("list", "max") and field is None:
            raise ValueError(f"derived.{name}: {func}() requires a <group>.<field>")
        specs.append(DerivedSpec(name=name, func=func, source=source, field=field))
    return tuple(specs)


def load_tenant_mapping(tenant: str, mappings_dir: Path | None = None) -> TenantMapping:
    """Load and validate config/tenants/<tenant>.yaml.

    Raises:
        ValueError: if the file is missing or fails validation.
    """
    directory = mappings_dir or _DEFAULT_MAPPINGS_DIR
    cache_key = (tenant, str(directory))
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    path = directory / f"{tenant}.yaml"
    if not path.is_file():
        raise ValueError(f"no mapping file for tenant {tenant!r} at {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    identity = raw.get("identity") or {}
    if "email" not in identity:
        raise ValueError(f"{path}: identity.email is required")

    repeating = _parse_repeating(raw.get("repeating"))
    mapping = TenantMapping(
        tenant=raw.get("tenant", tenant),
        email_source=identity["email"],
        external_id_source=identity.get("external_id"),
        vertical_source=identity.get("vertical"),
        fields={
            name: _field_spec(body, f"fields.{name}")
            for name, body in (raw.get("fields") or {}).items()
        },
        repeating=repeating,
        derived=_parse_derived(raw.get("derived"), repeating),
        consent=_parse_consent(raw.get("consent")),
    )
    _cache[cache_key] = mapping
    return mapping
