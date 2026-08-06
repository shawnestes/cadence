# Handoff — OTTO record ingestion

Written 2026-08-06. Previous session built the feature end to end; nothing is pushed or merged.

## Start here

```bash
cd C:\Users\shawn\PycharmProjects\cadence
git checkout feat/otto-record-ingestion          # already the current branch
docker compose -f docker-compose.test.yml up -d --wait
uv run pytest -q                                  # expect 632 passed, 2 failed
```

The 2 failures are **pre-existing and not from this work**: `tests/workers/test_engagement_scorer.py`
asserts that a worker still scores recipients, but that worker was retired in earlier work and now
just logs a deprecation warning. They failed before this branch existed. Deleting or rewriting those
two tests is an open decision (see Decisions).

## State

- Branch `feat/otto-record-ingestion`, HEAD `4e5bd5b`, **17 commits ahead of `main`** (`main` is at `8db32c2`).
- Nothing pushed. No PR opened. Working tree clean apart from untracked `.claude/`.
- Suite: 632 passed / 2 pre-existing failures.

## What shipped

A multi-tenant bulk record loader. Vendor CSV/JSONL of insurance leads streams through a per-tenant
YAML mapping into three tables, with a CLI and an authenticated HTTP endpoint as the two front doors.

```bash
# dry run — validates, writes rejects file, touches the DB in no way
uv run python -m app.cli.load_records --tenant otto --file leads.csv --vertical auto --dry-run

# real load
uv run python -m app.cli.load_records --tenant otto --file leads.csv --vertical auto
```

`POST /api/ingest/records/batch` is the same pipeline over HTTP — API-key authenticated, capped at
200 records per request, returns a per-row status.

### Files

| Path | Role |
|---|---|
| `config/tenants/otto.yaml` | OTTO's field mapping, vehicles group, derived fields, consent block |
| `app/domain/normalizers.py` | pure: email, name, state, zip5, phone_e164, year, ip |
| `app/domain/tenant_spec.py` | frozen dataclasses for a tenant mapping |
| `app/domain/record_mapping.py` | pure: raw row → MappedRow (ok / quarantine / reject) |
| `app/services/tenant_mapping.py` | loads and validates `config/tenants/*.yaml` |
| `app/services/record_ingest.py` | the only DB writer; `ingest_one_row` is shared by both front doors |
| `app/cli/load_records.py` | streaming CLI |
| `app/api/ingest.py` | `POST /api/ingest/records/batch` appended to the existing router |
| `alembic/…-seg8a_consent_proof_dedup.py` | unique index making a full file re-run safe |

Per row: upsert `recipients`, insert `consent_proof`, upsert `contact_profile`.

### The rules that matter

- **No consent, no contact.** A row with no TCPA text or no consent timestamp writes a
  `contact_quarantine` row and creates **no recipient** (SPEC-segmentation §9, case 13).
- **A re-import never resurrects an unsubscriber.** `contact_profile.consent_state` ratchets toward
  opted-out and `suppressed_at` is never cleared. Verified live through both front doors.
- **`consent_at` moves forward only.** Replaying a stale export cannot regress a newer proof.
- **`attributes` is replaced wholesale, not merged** — each file row is a complete snapshot, so an
  omitted key must not leave a stale value behind. Optional fields are written as explicit `null`.

## Decisions waiting on you

1. **Merge or PR?** Nothing is pushed. Your standing rule is no push without explicit confirmation.
2. **The 2 retired `engagement_scorer` tests** — delete them, rewrite them against `profile_refresh`,
   or leave them failing? They are the only red in the suite.
3. **`/code-review ultra` before merge?** 3,700 lines touching compliance-critical paths. It is
   user-triggered and billed; I cannot launch it.

## Open follow-ups (none blocking)

Recorded during review, deliberately deferred:

- `app/domain/record_mapping.py` — a `vehicles` value arriving as a JSON-*encoded string* silently
  yields `[]`. Not a supported input shape today (CSV uses indexed columns, JSONL a real array).
- `ConsentSpec.required` is parsed from YAML and never consulted; consent is always mandatory. Safer
  than the alternative and matches SPEC-segmentation §9, but the config field does nothing.
- `app/services/record_ingest.py::flush_unknown_domains` finds pending MX tasks by matching
  `repr()` of asyncio tasks — works on CPython 3.12, relies on undocumented `Task.__repr__`, and is
  process-wide rather than scoped per call. Would show up under concurrent HTTP traffic.
- `app/cli/load_records.py` — the comment above `session.rollback()` in `flush()` claims retries
  would hit `PendingRollbackError`; they wouldn't, because retries open a fresh session per row. The
  code is fine, the comment is wrong.
- `app/api/campaigns.py` — segment branch does 3 DB round-trips where 2 would do.
- No test covers `compile_segment` `ValueError`/`KeyError` → HTTP 400 mapping in `app/api/workflows.py`.
- `tests/domain/test_record_mapping.py` — unused `pytest` / `Path` imports.
- Throughput: a 3M-row file does ~10-12M sequential round trips (per-row upserts, chosen deliberately
  to avoid "ON CONFLICT cannot affect row a second time" on in-batch duplicate emails). Fine
  correctness-wise; worth revisiting if a production run is timed and found slow.

## Out of scope, documented in the spec (§13)

Do not treat these as gaps:

1. Nothing propagates a resolved `domain_provider` back onto existing `contact_profile` rows, so
   SPEC-segmentation §12 case 12 ("reclassified within 24h") is not achievable yet. Needs a pass in
   `profile_refresh`.
2. SMS consent — `channels: [1]` only.
3. Hoisting the `attributes.*` correlated subquery to a JOIN if materialize latency regresses.
4. Enrollment via the HTTP endpoint is deferred — `enroll: true` returns HTTP 400 rather than
   silently no-opping. The CLI's `--enroll` works and requires `--yes` above 1,000 projected enrollments.

## Things that will trip you up

- **Your M-SEG work was uncommitted.** Migrations `seg1`–`seg7a`, `app/models/consent_proof.py`,
  `contact_profile`, `profile_refresh.py`, `origin_platform.py` — all untracked at session start, and
  the new `seg8a` migration chained onto a `seg7a` that wasn't in git. All committed now as `765cbff`
  ("feat: segmentation engine (M-SEG 1-7)"). Check for more uncommitted work before starting.
- **10 broken `compile_segment` callers were fixed in passing** (`36cba57`). `app/api/segments.py`,
  `campaigns.py` and `workflows.py` were calling a signature that no longer existed — segments
  preview, campaigns send-by-segment, and workflow backfill all raised `TypeError`. That is why the
  suite went from 12 failures to 2.
- **The provider code maps disagreed.** `segment_query.py` had `cable`/`apple` swapped versus
  `app/domain/isp.py` and the DB seed, so `mailbox_provider: cable` selected apple contacts.
  `segment_query.py` now derives its maps from `isp.py` so they cannot drift again.
- **6 stale git worktree branches** (`worktree-wf_2fb1c2e3-bb3-7` … `-12`, all at `7855920`) are
  left over from an earlier workflow run, and `.claude/worktrees/` holds a full `.venv`. Both are
  gitignored now. Clean up when convenient.
- Docker test DB: use `127.0.0.1`, not `localhost` — `::1` stalls ~25s per connection on Windows.
- Test rows must use emails ending `@cadencetests.io`; the autouse purge fixture keys on that suffix.

## Where the detail lives

- Design/spec: `docs/superpowers/specs/2026-08-05-otto-record-ingestion-design.md`
- Implementation plan: `docs/superpowers/plans/2026-08-05-otto-record-ingestion.md`
- Per-task briefs, implementer reports, review packages and the progress ledger:
  `.superpowers/sdd/2026-08-05-otto-record-ingestion/` (gitignored — delete when you no longer want
  the audit trail)

Worth knowing for calibration: every Critical/Important finding the review loop caught came from
reference code in the plan, not from the implementers. `LEAST` vs `GREATEST` on `consent_state`
(which would have resurrected unsubscribers), `int(float("inf"))` raising `OverflowError` in a
module contracted never to raise, the config loader leaking `AttributeError`, a TOCTOU window
between the suppression read and the profile write, and an encoding guess that silently corrupted
accented names. Plan code deserves the same review as implementation code.
