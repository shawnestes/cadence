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
import codecs
import csv
import json
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._enrollment import enroll_recipient
from app.core.db import async_session_factory, engine
from app.domain.record_mapping import OUTCOME_OK, OUTCOME_QUARANTINE, map_row
from app.domain.segment import matches as segment_matches
from app.models.recipient import Recipient
from app.models.workflow import Workflow
from app.services.record_ingest import (
    IngestCounters,
    flush_unknown_domains,
    ingest_one_row,
    ingest_record,
    quarantine_row,
)
from app.services.tenant_mapping import load_tenant_mapping

log = structlog.get_logger(__name__)

_ENROLL_CONFIRM_THRESHOLD = 1000

# Probe size for encoding auto-detection: large enough that real files are
# very unlikely to be entirely ASCII/valid-UTF-8 for this long and then
# switch encodings, small enough to read cheaply up front.
_PROBE_SIZE = 1024 * 1024  # 1 MiB


@dataclass
class _BatchEffects:
    """Everything a batch would record if — and only if — its commit lands.

    _process buffers here instead of mutating the run-wide counters, rejects
    file and unknown-domain set directly. A batch that raises is rolled back
    and every row retried individually, so side effects applied during the
    doomed first pass would be counted (and written to the rejects CSV) twice
    for each row that had already succeeded before the failing one.
    """

    loaded: int = 0
    updated: int = 0
    quarantined: int = 0
    rejected: int = 0
    enrolled: int = 0
    error_rows: list[list[Any]] = field(default_factory=list)
    unknown_domains: set[str] = field(default_factory=set)

    def apply(
        self, counters: IngestCounters, errors: Any, unknown_domains: set[str]
    ) -> None:
        counters.loaded += self.loaded
        counters.updated += self.updated
        counters.quarantined += self.quarantined
        counters.rejected += self.rejected
        counters.enrolled += self.enrolled
        for row in self.error_rows:
            errors.writerow(row)
        unknown_domains.update(self.unknown_domains)


class RowDecodingError(Exception):
    """The resolved encoding failed to decode the file at some point.

    Distinguished from a wrong initial guess (which _detect_encoding exists
    to avoid): this is a byte sequence found mid-stream, past whatever was
    checked (by probe or by the operator's own judgment), that doesn't
    decode. Abort loudly rather than silently substituting a replacement
    character, which would store corrupted PII.
    """

    def __init__(self, *, encoding: str, row_number: int, cause: UnicodeDecodeError) -> None:
        self.encoding = encoding
        self.row_number = row_number
        self.cause = cause
        super().__init__(
            f"failed to decode input as {encoding!r} at or after row {row_number} "
            f"(byte offset {cause.start} of the failing read): {cause.reason}. "
            f"Re-run with an explicit --encoding if {encoding!r} is wrong for this file."
        )


def _detect_encoding(path: Path) -> str:
    """Guess "utf-8-sig" vs "cp1252" by probing the first _PROBE_SIZE bytes.

    UTF-8 is self-validating: real-world cp1252 text containing accented
    characters essentially never happens to also form valid UTF-8. A clean
    probe decode means utf-8-sig (which also transparently strips a BOM, or
    behaves as plain UTF-8 if there isn't one); a UnicodeDecodeError on the
    probe means cp1252.

    Uses an incremental decoder with final=False so a multi-byte UTF-8
    sequence that happens to be split by the probe's chunk boundary isn't
    mistaken for invalid input — only a byte sequence that can never be
    valid UTF-8, regardless of what bytes might follow, raises.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    with path.open("rb") as fh:
        chunk = fh.read(_PROBE_SIZE)
    try:
        decoder.decode(chunk, final=False)
    except UnicodeDecodeError:
        return "cp1252"
    return "utf-8-sig"


def read_rows(
    path: Path, encoding: str = "auto"
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Stream (row_number, row) from a .csv or .jsonl file.

    encoding="auto" (the default) probes the file via _detect_encoding to
    choose "utf-8-sig" or "cp1252"; an explicit encoding name is used
    exactly as given, with no probing. Decoding is strict — a byte sequence
    that can't decode raises RowDecodingError rather than silently
    substituting the Unicode replacement character, which would store
    corrupted PII. newline="" is required by the csv module so embedded
    newlines inside quoted fields survive on Windows.
    """
    resolved = _detect_encoding(path) if encoding == "auto" else encoding
    suffix = path.suffix.lower()
    n = 0
    try:
        with path.open("r", newline="", encoding=resolved) as fh:
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
    except UnicodeDecodeError as exc:
        raise RowDecodingError(encoding=resolved, row_number=n + 1, cause=exc) from exc


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
                    effects = await _process(rows, session)
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
                    # The whole batch is gone, so nothing the doomed pass
                    # buffered counts. Only the retries below are recorded.
                    for row in rows:
                        async with async_session_factory() as row_session:
                            try:
                                row_effects = await _process([row], row_session)
                                await row_session.commit()
                            except Exception as row_exc:  # noqa: BLE001
                                await row_session.rollback()
                                counters.rejected += 1
                                errors.writerow(
                                    [row[0], "error", str(row_exc)[:500], json.dumps(row[1])]
                                )
                            else:
                                row_effects.apply(counters, errors, unknown_domains)
                else:
                    effects.apply(counters, errors, unknown_domains)
            log.info(
                "load_records.batch_complete",
                batch_start=batch_start,
                elapsed_ms=int((time.monotonic() - batch_began) * 1000),
                **counters.as_dict(),
            )

        async def _process(
            rows: list[tuple[int, dict[str, Any]]], session: AsyncSession
        ) -> _BatchEffects:
            effects = _BatchEffects()
            now = datetime.now(timezone.utc)
            for row_number, raw in rows:
                # ingest_record_fn/quarantine_row_fn are passed explicitly
                # (rather than relying on ingest_one_row's own defaults) so
                # that monkeypatching this module's `ingest_record` name (as
                # the batch-failure test does) is observed here too.
                outcome, record = await ingest_one_row(
                    raw,
                    session,
                    tenant=args.tenant,
                    vertical=args.vertical,
                    mapping=mapping,
                    now=now,
                    ingest_record_fn=ingest_record,
                    quarantine_row_fn=quarantine_row,
                )
                if outcome.unknown_domain:
                    effects.unknown_domains.add(outcome.unknown_domain)
                if outcome.status == "created":
                    effects.loaded += 1
                elif outcome.status == "updated":
                    effects.updated += 1
                elif outcome.status == "quarantined":
                    effects.quarantined += 1
                    effects.error_rows.append(
                        [row_number, "quarantine", "; ".join(outcome.errors), json.dumps(raw)]
                    )
                else:
                    effects.rejected += 1
                    effects.error_rows.append(
                        [row_number, "reject", "; ".join(outcome.errors), json.dumps(raw)]
                    )

                if args.enroll and record is not None:
                    contact_id = (
                        await session.execute(
                            select(Recipient.id)
                            .where(Recipient.tenant == args.tenant)
                            .where(Recipient.email == record.email)
                        )
                    ).scalar_one()
                    effects.enrolled += await _enroll(
                        session,
                        args.tenant,
                        contact_id,
                        record.attributes,
                        args.vertical,
                        now,
                    )
            return effects

        batch_start = 1
        try:
            for row_number, raw in read_rows(path, encoding=args.encoding):
                counters.read += 1
                if args.limit and counters.read > args.limit:
                    break
                if args.dry_run:
                    mapped = map_row(raw, mapping)
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
        except RowDecodingError as exc:
            # Flush whatever already-decoded rows made it into the pending
            # batch before aborting — they decoded fine and shouldn't be
            # thrown away because a later byte in the file didn't.
            if not args.dry_run:
                await flush(batch, batch_start)
            print(f"error: {exc}", file=sys.stderr)
            return 2
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
        "--encoding",
        default="auto",
        help=(
            "input file text encoding. 'auto' (default) probes the first "
            "1 MiB for valid UTF-8 and falls back to cp1252; an explicit "
            "codec name (e.g. cp1252) is used verbatim with no probing."
        ),
    )
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
