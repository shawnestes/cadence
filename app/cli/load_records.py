"""Bulk-load tenant records from a CSV or JSONL file.

    uv run python -m app.cli.load_records \
        --tenant otto --file leads.csv --vertical auto

Rows without resolvable TCPA consent are written to contact_quarantine and no
recipients row is created (SPEC-segmentation §9). Rows that cannot be parsed at
all go to the rejects file only.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._enrollment import enroll_recipient
from app.core.db import async_session_factory, engine
from app.domain.record_mapping import (
    OUTCOME_OK,
    OUTCOME_QUARANTINE,
    OUTCOME_REJECT,
    MappedRow,
    map_row,
)
from app.domain.segment import matches as segment_matches
from app.domain.tenant_spec import TenantMapping
from app.models.recipient import Recipient
from app.models.workflow import Workflow
from app.services.record_ingest import (
    IngestCounters,
    flush_unknown_domains,
    ingest_record,
    quarantine_row,
)
from app.services.tenant_mapping import load_tenant_mapping

log = structlog.get_logger(__name__)

_ENROLL_CONFIRM_THRESHOLD = 1000

# errors="replace" in read_rows silently substitutes this for any byte
# sequence that can't decode as UTF-8 (e.g. a cp1252 file with no BOM).
# Accepting a row containing it would store corrupted PII; reject instead.
_REPLACEMENT_CHAR = "�"


def _map_row_checked(raw: dict[str, Any], mapping: TenantMapping) -> MappedRow:
    """map_row, but reject first if decoding already corrupted any value."""
    bad_columns = sorted(
        key
        for key, value in raw.items()
        if isinstance(value, str) and _REPLACEMENT_CHAR in value
    )
    if bad_columns:
        return MappedRow(
            outcome=OUTCOME_REJECT,
            errors=(
                "invalid encoding (unicode replacement character) in "
                f"column(s): {', '.join(bad_columns)}",
            ),
            record=None,
        )
    return map_row(raw, mapping)


def read_rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Stream (row_number, row) from a .csv or .jsonl file.

    utf-8-sig transparently handles a BOM or its absence; newline="" is
    required by the csv module so embedded newlines inside quoted fields
    survive on Windows.
    """
    suffix = path.suffix.lower()
    with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as fh:
        if suffix in (".jsonl", ".ndjson"):
            for n, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield n, json.loads(line)
                except json.JSONDecodeError as exc:
                    yield n, {"__parse_error__": str(exc)}
        else:
            for n, row in enumerate(csv.DictReader(fh), start=1):
                yield n, dict(row)


async def _project_enrollment(tenant: str) -> int:
    async with async_session_factory() as session:
        workflows = (
            await session.execute(
                select(Workflow)
                .where(Workflow.tenant == tenant)
                .where(Workflow.status == "active")
            )
        ).scalars().all()
    return len(workflows)


async def _enroll(
    session: AsyncSession,
    tenant: str,
    contact_id: uuid.UUID,
    attributes: dict,
    vertical: str,
    now: datetime,
) -> int:
    workflows = (
        await session.execute(
            select(Workflow)
            .where(Workflow.tenant == tenant)
            .where(Workflow.status == "active")
        )
    ).scalars().all()
    enrolled = 0
    for wf in workflows:
        if not segment_matches(wf.entry_segment, attributes, vertical):
            continue
        await enroll_recipient(session, wf, contact_id, tenant, now)
        enrolled += 1
    return enrolled


async def run(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2

    mapping = load_tenant_mapping(args.tenant)
    counters = IngestCounters()
    errors_path = Path(args.errors_out)
    errors_path.parent.mkdir(parents=True, exist_ok=True)

    if args.enroll and not args.dry_run:
        workflow_count = await _project_enrollment(args.tenant)
        print(
            f"--enroll: rows will be evaluated against {workflow_count} active workflows"
        )
        if not args.yes:
            print(
                "refusing to enroll without --yes "
                f"(threshold {_ENROLL_CONFIRM_THRESHOLD} enrollments)",
                file=sys.stderr,
            )
            return 2

    unknown_domains: set[str] = set()
    batch: list[tuple[int, dict[str, Any]]] = []
    started = time.monotonic()

    with errors_path.open("w", newline="", encoding="utf-8") as errors_fh:
        errors = csv.writer(errors_fh)
        errors.writerow(["row_number", "outcome", "errors", "raw"])

        async def flush(rows: list[tuple[int, dict[str, Any]]], batch_start: int) -> None:
            if not rows:
                return
            batch_began = time.monotonic()
            async with async_session_factory() as session:
                try:
                    await _process(rows, session)
                    await session.commit()
                except Exception as exc:  # noqa: BLE001
                    # A failed statement poisons the session; without the
                    # rollback every retried row raises PendingRollbackError.
                    await session.rollback()
                    log.warning(
                        "load_records.batch_failed_retrying_rows",
                        batch_start=batch_start,
                        error=str(exc),
                    )
                    for row in rows:
                        async with async_session_factory() as row_session:
                            try:
                                await _process([row], row_session)
                                await row_session.commit()
                            except Exception as row_exc:  # noqa: BLE001
                                await row_session.rollback()
                                counters.rejected += 1
                                errors.writerow(
                                    [row[0], "error", str(row_exc)[:500], json.dumps(row[1])]
                                )
            log.info(
                "load_records.batch_complete",
                batch_start=batch_start,
                elapsed_ms=int((time.monotonic() - batch_began) * 1000),
                **counters.as_dict(),
            )

        async def _process(
            rows: list[tuple[int, dict[str, Any]]], session: AsyncSession
        ) -> None:
            now = datetime.now(timezone.utc)
            for row_number, raw in rows:
                mapped = _map_row_checked(raw, mapping)
                if mapped.outcome == OUTCOME_OK:
                    status, unknown = await ingest_record(
                        session,
                        tenant=args.tenant,
                        vertical=args.vertical,
                        record=mapped.record,
                        now=now,
                    )
                    if unknown:
                        unknown_domains.add(unknown)
                    if status == "created":
                        counters.loaded += 1
                    else:
                        counters.updated += 1
                    if args.enroll:
                        contact_id = (
                            await session.execute(
                                select(Recipient.id)
                                .where(Recipient.tenant == args.tenant)
                                .where(Recipient.email == mapped.record.email)
                            )
                        ).scalar_one()
                        counters.enrolled += await _enroll(
                            session,
                            args.tenant,
                            contact_id,
                            mapped.record.attributes,
                            args.vertical,
                            now,
                        )
                elif mapped.outcome == OUTCOME_QUARANTINE:
                    await quarantine_row(
                        session,
                        tenant=args.tenant,
                        reason="; ".join(mapped.errors),
                        raw_row=raw,
                        now=now,
                    )
                    counters.quarantined += 1
                    errors.writerow(
                        [row_number, "quarantine", "; ".join(mapped.errors), json.dumps(raw)]
                    )
                else:
                    counters.rejected += 1
                    errors.writerow(
                        [row_number, "reject", "; ".join(mapped.errors), json.dumps(raw)]
                    )

        batch_start = 1
        for row_number, raw in read_rows(path):
            counters.read += 1
            if args.limit and counters.read > args.limit:
                break
            if args.dry_run:
                mapped = _map_row_checked(raw, mapping)
                if mapped.outcome == OUTCOME_OK:
                    counters.loaded += 1
                elif mapped.outcome == OUTCOME_QUARANTINE:
                    counters.quarantined += 1
                    errors.writerow(
                        [row_number, "quarantine", "; ".join(mapped.errors), json.dumps(raw)]
                    )
                else:
                    counters.rejected += 1
                    errors.writerow(
                        [row_number, "reject", "; ".join(mapped.errors), json.dumps(raw)]
                    )
                continue
            batch.append((row_number, raw))
            if len(batch) >= args.batch_size:
                await flush(batch, batch_start)
                batch = []
                batch_start = row_number + 1
        if not args.dry_run:
            await flush(batch, batch_start)

    if not args.dry_run:
        await flush_unknown_domains(args.tenant, unknown_domains)

    summary = counters.as_dict()
    print(" ".join(f"{k}={v}" for k, v in summary.items()))
    log.info(
        "load_records.complete",
        tenant=args.tenant,
        file=str(path),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        dry_run=args.dry_run,
        **summary,
    )

    failed = counters.quarantined + counters.rejected
    if counters.read and failed / counters.read > args.max_error_rate:
        print(
            f"error rate {failed / counters.read:.2%} exceeds "
            f"--max-error-rate {args.max_error_rate:.2%}",
            file=sys.stderr,
        )
        return 2
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="load_records", description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--vertical", required=True, choices=["auto", "home"])
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument(
        "--errors-out",
        default=str(
            Path("import-logs")
            / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-rejects.csv"
        ),
        help="rejects file; contains PII and consent text",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-error-rate", type=float, default=0.25)
    parser.add_argument("--enroll", action="store_true")
    parser.add_argument("--yes", action="store_true", help="confirm --enroll")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    async def _main() -> int:
        try:
            return await run(args)
        finally:
            await engine.dispose()

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
