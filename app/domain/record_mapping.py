"""Map one raw tenant record onto Cadence's contact shape.

Pure: no FastAPI, no SQLAlchemy. Takes a dict (one CSV row or one JSONL
object) plus a TenantMapping and returns a MappedRow describing whether the
row can be loaded, must be quarantined for lack of consent, or is rejected
outright.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.domain.normalizers import NORMALIZERS, normalize_email, normalize_year
from app.domain.tenant_spec import DerivedSpec, RepeatingSpec, TenantMapping

_WHITESPACE = re.compile(r"\s+")

OUTCOME_OK = "ok"
OUTCOME_QUARANTINE = "quarantine"
OUTCOME_REJECT = "reject"


@dataclass(frozen=True)
class ConsentRecord:
    text: str
    captured_at: datetime
    channels: tuple[int, ...]
    source_url: str | None = None
    form_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True)
class NormalizedRecord:
    email: str
    external_id: str | None
    vertical: str | None
    attributes: dict[str, Any]
    consent: ConsentRecord
    consent_source: str


@dataclass(frozen=True)
class MappedRow:
    outcome: str
    errors: tuple[str, ...]
    record: NormalizedRecord | None


def disclosure_hash(text: str) -> str:
    """SHA-256 of the disclosure text, whitespace-normalized.

    Whitespace normalization keeps a re-export with different line wrapping
    from looking like a different disclosure.
    """
    return hashlib.sha256(
        _WHITESPACE.sub(" ", text).strip().encode("utf-8")
    ).hexdigest()


def _raw(row: Mapping[str, Any], key: str) -> Any:
    value = row.get(key)
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _apply(spec_normalize: str | None, value: Any) -> Any:
    if value is None:
        return None
    if spec_normalize is None:
        s = str(value).strip()
        return s or None
    return NORMALIZERS[spec_normalize](value)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive input is treated as UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _collect_vehicles(row: Mapping[str, Any], spec: RepeatingSpec) -> list[dict[str, Any]]:
    """Read a repeating group from either indexed columns or a JSON array."""
    raw_items: list[Mapping[str, Any]] = []
    json_value = row.get(spec.json_key)
    if isinstance(json_value, list):
        raw_items = [item for item in json_value if isinstance(item, Mapping)]
    else:
        for n in range(1, spec.max + 1):
            item = {
                field: _raw(row, spec.column_template.format(n=n, field=field))
                for field in spec.fields
            }
            if any(v is not None for v in item.values()):
                raw_items.append(item)

    items: list[dict[str, Any]] = []
    for raw_item in raw_items[: spec.max]:
        item: dict[str, Any] = {}
        for field in spec.fields:
            value = raw_item.get(field)
            if isinstance(value, str) and not value.strip():
                value = None
            if value is None:
                continue
            item[field] = normalize_year(value) if field == "year" else str(value).strip()
        if item:
            items.append(item)
    return items


def _derive(spec: DerivedSpec, groups: dict[str, list[dict[str, Any]]]) -> Any:
    items = groups.get(spec.source, [])
    if spec.func == "count":
        return len(items)
    values = [item.get(spec.field) for item in items]
    if spec.func == "list":
        out: list[str] = []
        for value in values:
            if value is None:
                continue
            lowered = str(value).lower()
            if lowered not in out:
                out.append(lowered)
        return out
    # max
    numeric = [v for v in values if isinstance(v, int)]
    return max(numeric) if numeric else None


def map_row(row: Mapping[str, Any], mapping: TenantMapping) -> MappedRow:
    """Map one raw record. Never raises on bad data — it reports."""
    errors: list[str] = []

    email = normalize_email(_raw(row, mapping.email_source))
    if email is None:
        return MappedRow(
            outcome=OUTCOME_REJECT,
            errors=(f"invalid or missing email in column {mapping.email_source!r}",),
            record=None,
        )

    attributes: dict[str, Any] = {}
    for name, spec in mapping.fields.items():
        value = _apply(spec.normalize, _raw(row, spec.source))
        if value is None and spec.required:
            errors.append(f"missing or invalid required field {name!r} (column {spec.source!r})")
        attributes[name] = value

    external_id = None
    if mapping.external_id_source:
        external_id = _apply(None, _raw(row, mapping.external_id_source))
        attributes[mapping.external_id_source] = external_id

    groups: dict[str, list[dict[str, Any]]] = {
        spec.name: _collect_vehicles(row, spec) for spec in mapping.repeating
    }
    for name, items in groups.items():
        attributes[name] = items
    for derived in mapping.derived:
        attributes[derived.name] = _derive(derived, groups)

    vertical = None
    if mapping.vertical_source:
        vertical = _apply(None, _raw(row, mapping.vertical_source))

    if errors:
        return MappedRow(outcome=OUTCOME_REJECT, errors=tuple(errors), record=None)

    consent_spec = mapping.consent
    text = _apply(None, _raw(row, consent_spec.text.source))
    captured_at = _parse_timestamp(_raw(row, consent_spec.captured_at.source))
    consent_errors: list[str] = []
    if text is None:
        consent_errors.append(f"missing consent text (column {consent_spec.text.source!r})")
    if captured_at is None:
        consent_errors.append(
            f"missing or unparseable consent timestamp "
            f"(column {consent_spec.captured_at.source!r})"
        )
    if consent_errors:
        return MappedRow(
            outcome=OUTCOME_QUARANTINE, errors=tuple(consent_errors), record=None
        )

    optional = {
        key: _apply(spec.normalize, _raw(row, spec.source))
        for key, spec in consent_spec.optional.items()
    }

    record = NormalizedRecord(
        email=email,
        external_id=external_id,
        vertical=vertical,
        attributes=attributes,
        consent=ConsentRecord(
            text=text,
            captured_at=captured_at,
            channels=consent_spec.channels,
            source_url=optional.get("source_url"),
            form_id=optional.get("form_id"),
            ip=optional.get("ip"),
            user_agent=optional.get("user_agent"),
        ),
        consent_source=consent_spec.source_label,
    )
    return MappedRow(outcome=OUTCOME_OK, errors=(), record=record)
