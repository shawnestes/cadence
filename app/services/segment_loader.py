"""YAML segment loader with spec_hash upsert (SPEC-segmentation.md §3).

Reads all *.yaml files under segments/, parses each segment definition,
computes a spec_hash, and upserts into segment_definition.

No FastAPI imports. Uses sqlalchemy.ext.asyncio for DB access.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import structlog
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

# Repo root is 3 levels up from app/services/segment_loader.py:
#   app/services/segment_loader.py -> app/services -> app -> repo_root
_REPO_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_SEGMENTS_DIR = _REPO_ROOT / "segments"

_CHANNEL_MAP: dict[str, int] = {
    "email": 1,
    "sms": 2,
}

_UPSERT_SQL = text(
    """
    INSERT INTO segment_definition
        (tenant, key, name, channel, predicate, spec_hash,
         active, acknowledges_cold, include_unsendable, updated_at)
    VALUES
        (:tenant, :key, :name, :channel, CAST(:predicate AS jsonb), :spec_hash,
         true, :acknowledges_cold, :include_unsendable, now())
    ON CONFLICT (tenant, key) DO UPDATE SET
        name               = EXCLUDED.name,
        channel            = EXCLUDED.channel,
        predicate          = EXCLUDED.predicate,
        spec_hash          = EXCLUDED.spec_hash,
        active             = true,
        acknowledges_cold  = EXCLUDED.acknowledges_cold,
        include_unsendable = EXCLUDED.include_unsendable,
        updated_at         = now()
    RETURNING segment_id, spec_hash,
              (SELECT spec_hash FROM segment_definition
               WHERE tenant = EXCLUDED.tenant AND key = EXCLUDED.key
                 AND spec_hash <> EXCLUDED.spec_hash) AS old_hash
    """
)

# Simpler approach: fetch old hash before upsert
_FETCH_EXISTING_SQL = text(
    """
    SELECT key, spec_hash FROM segment_definition
    WHERE tenant = :tenant AND key = ANY(:keys)
    """
)

_UPSERT_SIMPLE_SQL = text(
    """
    INSERT INTO segment_definition
        (tenant, key, name, channel, predicate, spec_hash,
         active, acknowledges_cold, include_unsendable, updated_at)
    VALUES
        (:tenant, :key, :name, :channel, CAST(:predicate AS jsonb), :spec_hash,
         true, :acknowledges_cold, :include_unsendable, now())
    ON CONFLICT (tenant, key) DO UPDATE SET
        name               = EXCLUDED.name,
        channel            = EXCLUDED.channel,
        predicate          = EXCLUDED.predicate,
        spec_hash          = EXCLUDED.spec_hash,
        active             = true,
        acknowledges_cold  = EXCLUDED.acknowledges_cold,
        include_unsendable = EXCLUDED.include_unsendable,
        updated_at         = now()
    """
)


def _compute_spec_hash(predicate: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the raw predicate dict."""
    return hashlib.sha256(
        json.dumps(predicate, sort_keys=True).encode()
    ).hexdigest()


def _tenant_from_path(yaml_path: Path, segments_dir: Path) -> str:
    """Derive tenant from first path component under segments_dir."""
    try:
        rel = yaml_path.relative_to(segments_dir)
        parts = rel.parts
        if len(parts) > 1:
            return parts[0]
    except ValueError:
        pass
    return "otto"


def _parse_yaml_file(
    yaml_path: Path, segments_dir: Path
) -> list[dict[str, Any]]:
    """Parse a segment YAML file and return a list of normalized segment dicts."""
    with yaml_path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)

    entries: list[dict[str, Any]] = doc.get("segments", []) or []
    results: list[dict[str, Any]] = []

    default_tenant = _tenant_from_path(yaml_path, segments_dir)

    for entry in entries:
        channel_str: str = entry.get("channel", "email")
        channel_int: int = _CHANNEL_MAP.get(channel_str, 1)
        predicate: dict[str, Any] = entry.get("predicate") or {}
        tenant: str = entry.get("tenant", default_tenant)

        results.append(
            {
                "tenant": tenant,
                "key": entry["key"],
                "name": entry.get("name", entry["key"]),
                "channel": channel_int,
                "predicate": predicate,
                "spec_hash": _compute_spec_hash(predicate),
                "acknowledges_cold": bool(entry.get("acknowledges_cold", False)),
                "include_unsendable": bool(entry.get("include_unsendable", False)),
            }
        )

    return results


async def load_segments(
    db_conn: AsyncSession,
    *,
    segments_dir: Path | None = None,
) -> dict[str, Any]:
    """Load all segment YAML files and upsert into segment_definition.

    Args:
        db_conn: Async SQLAlchemy session (AsyncSession).
        segments_dir: Directory to scan for *.yaml files. Defaults to
            repo_root/segments/.

    Returns:
        Summary dict: {"upserted": int, "unchanged": int, "flagged_rebuild": int}.
    """
    resolved_dir = segments_dir if segments_dir is not None else _DEFAULT_SEGMENTS_DIR

    # Collect all segment dicts from all YAML files
    all_segments: list[dict[str, Any]] = []
    yaml_files = sorted(resolved_dir.rglob("*.yaml"))

    for yaml_path in yaml_files:
        entries = _parse_yaml_file(yaml_path, resolved_dir)
        log.info(
            "segment_loader.file_loaded",
            path=str(yaml_path),
            count=len(entries),
        )
        all_segments.extend(entries)

    if not all_segments:
        log.info(
            "segment_loader.done",
            upserted=0,
            unchanged=0,
            flagged_rebuild=0,
        )
        return {"upserted": 0, "unchanged": 0, "flagged_rebuild": 0}

    # Fetch existing spec_hashes grouped by (tenant, key) to detect changes.
    # Group segments by tenant for efficient batch lookup.
    by_tenant: dict[str, list[dict[str, Any]]] = {}
    for seg in all_segments:
        by_tenant.setdefault(seg["tenant"], []).append(seg)

    existing_hashes: dict[tuple[str, str], str] = {}
    for tenant, segs in by_tenant.items():
        keys = [s["key"] for s in segs]
        rows = await db_conn.execute(
            _FETCH_EXISTING_SQL,
            {"tenant": tenant, "keys": keys},
        )
        for row in rows:
            existing_hashes[(tenant, row.key)] = row.spec_hash

    # Upsert each segment and track outcomes.
    upserted = 0
    unchanged = 0
    flagged_rebuild = 0

    for seg in all_segments:
        lookup_key = (seg["tenant"], seg["key"])
        old_hash = existing_hashes.get(lookup_key)
        new_hash = seg["spec_hash"]

        await db_conn.execute(
            _UPSERT_SIMPLE_SQL,
            {
                "tenant": seg["tenant"],
                "key": seg["key"],
                "name": seg["name"],
                "channel": seg["channel"],
                "predicate": json.dumps(seg["predicate"]),
                "spec_hash": new_hash,
                "acknowledges_cold": seg["acknowledges_cold"],
                "include_unsendable": seg["include_unsendable"],
            },
        )

        if old_hash is None:
            # New segment
            upserted += 1
        elif old_hash == new_hash:
            unchanged += 1
        else:
            # Hash changed — predicate was modified
            upserted += 1
            flagged_rebuild += 1
            log.info(
                "segment_loader.spec_hash_changed",
                key=seg["key"],
                tenant=seg["tenant"],
                old_hash=old_hash,
                new_hash=new_hash,
            )

    await db_conn.commit()

    log.info(
        "segment_loader.done",
        upserted=upserted,
        unchanged=unchanged,
        flagged_rebuild=flagged_rebuild,
        total=len(all_segments),
    )
    return {"upserted": upserted, "unchanged": unchanged, "flagged_rebuild": flagged_rebuild}
