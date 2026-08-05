# OTTO record ingestion — design

Date: 2026-08-05
Status: approved design, pending implementation plan
Scope: multi-tenant bulk record ingestion, CLI first, HTTP endpoint second. OTTO is tenant #1.

## 1. Problem

OTTO lead records (user id, name, address, state, zip, phone, one or more vehicles, TCPA
consent text and timestamp) arrive as CSV/JSONL files. Cadence has a single-record ingest
endpoint (`app/api/ingest.py`) but no bulk path, no per-tenant field mapping, and nothing in
`app/` writes `consent_proof`, `contact_quarantine`, or `contact_profile`. This design adds
the bulk path and makes it the first real writer of the consent tables.

Tenant #2 will have a different record shape. The mapping mechanism, not the OTTO parser, is
the deliverable.

## 2. Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | Input is a CSV or JSONL file on disk, streamed | Files are how OTTO delivers today |
| D2 | Per-tenant shape lives in `config/tenants/<tenant>.yaml` | New tenant = new YAML, no code |
| D3 | TCPA consent text + timestamp arrive per record | Consent is per-lead, not per-batch |
| D4 | Verbatim disclosure text stored in `consent_proof.raw`; SHA-256 in `disclosure_text_hash` | TCPA burden of proof is on the sender (SPEC-segmentation §10) |
| D5 | Loader writes the full path: `recipients` + `consent_proof` + `contact_profile` | Contacts are segmentable on load, not after an unspecified backfill |
| D6 | No consent = no contact: `contact_quarantine` row, no `recipients` row | SPEC-segmentation §9, cases 13 and 34 |
| D7 | Workflow enrollment is off by default, `--enroll` opts in, with a preview + typed confirmation | Journeys cannot be recalled |
| D8 | `attributes.*` becomes a first-class segment field in the SQL compiler | Otherwise no OTTO vehicle data is segmentable |
| D9 | No resume ledger; `consent_proof` inserts are deduped so a full re-run is safe | Simpler than a checkpoint file or an `import_run` table |
| D10 | `segment_query.py` provider code maps are corrected to match `app/domain/isp.py` and the DB seed as part of this work | This feature writes codes that the segment layer currently misreads |

## 3. Files

New:

```
config/tenants/otto.yaml         # field + consent mapping
app/domain/normalizers.py        # pure: email, phone_e164, zip5, state, name, year
app/domain/record_mapping.py     # pure: raw dict -> NormalizedRecord | RowErrors
app/services/tenant_mapping.py   # loads and caches config/tenants/*.yaml
app/services/record_ingest.py    # async: recipients + consent_proof + contact_profile
app/cli/__init__.py
app/cli/load_records.py          # argparse; stdlib only, no new dependency
alembic/versions/<rev>-consent_proof_dedup_index.py
```

Modified:

```
app/services/segment_query.py    # attributes.* compilation; provider code map fix
app/domain/segment.py            # `contains` operator for list-valued fields
app/api/ingest.py                # route the batch endpoint through record_ingest (phase 2)
tests/workers/test_segment_materialize.py  # seeds use the corrected codes
.gitignore                       # import-logs/
```

`app/domain/normalizers.py` and `app/domain/record_mapping.py` import no FastAPI and no
SQLAlchemy (CLAUDE.md working principle 4). All function signatures carry type hints.

## 4. Tenant mapping YAML

```yaml
tenant: otto
identity:
  email: email
  external_id: user_id
fields:
  first_name:  {from: first_name, normalize: name}
  last_name:   {from: last_name,  normalize: name}
  address1:    {from: address}
  city:        {from: city}
  state:       {from: state, normalize: state}     # -> "TX"
  zip:         {from: zip,   normalize: zip5}      # -> "78701", leading zeros preserved
  phone:       {from: phone, normalize: phone_e164, required: false}
repeating:
  vehicles:
    columns: "vehicle_{n}_{field}"   # CSV: vehicle_1_year, vehicle_1_make, ...
    json_key: vehicles               # JSONL: a real array
    fields: [year, make, model, vin]
    max: 5
derived:
  vehicle_count:       count(vehicles)
  vehicle_makes:       list(vehicles.make)     # lowercased
  newest_vehicle_year: max(vehicles.year)      # int | None
consent:
  channels: [1]                                # email only; 2 requires SMS-covering text
  required: true
  text:        {from: tcpa_text}
  captured_at: {from: tcpa_consent_at}
  source_url:  {from: consent_url, required: false}
  form_id:     {from: lead_id,     required: false}
  ip:          {from: consent_ip,  required: false}
  user_agent:  {from: consent_ua,  required: false}
  source_label: otto_tcpa                      # -> recipients.consent_source
```

`vertical` comes from the `--vertical` CLI flag unless the YAML declares a mapped column,
in which case the column wins.

Derived-field semantics, all evaluated after per-vehicle normalization:

- `count(vehicles)` — number of vehicles that produced at least one non-empty field.
- `list(vehicles.make)` — lowercased, de-duplicated, order preserved.
- `max(vehicles.year)` — years coerced to `int | None` first; `None`s filtered; empty
  input yields `None`, never a `ValueError` and never a string comparison.

## 5. Write path

Per row, inside one transaction:

1. **`recipients` upsert** on `(tenant, email)`. Same statement shape as
   `app/api/ingest.py:43-64` with one deliberate difference: `attributes` is **replaced
   wholesale**, not merged with `||`. Each OTTO row is a complete snapshot; a shallow merge
   leaves a stale phone number in place when the new record omits one.
   `consent_at` and `consent_source` come from the record.
2. **`consent_proof` insert.** `disclosure_text_hash` = SHA-256 of the whitespace-normalized
   disclosure text. `raw` = `{"disclosure_text": <verbatim>, "row": <original row>}`.
   `channels` from the YAML. `ip`, `user_agent`, `source_url`, `form_id` when present.
   `ON CONFLICT (tenant, contact_id, disclosure_text_hash, captured_at) DO NOTHING` — see §7.
3. **`contact_profile` upsert** on `(tenant, contact_id, channel=1)`, raw SQL via
   `sqlalchemy.text()` to match every existing writer (`profile_refresh.py`,
   `origin_platform.py`); the table has a `GENERATED ALWAYS AS` column and no ORM model.
   Sets `email_domain`, `mailbox_provider`, `reputation_group`, `consent_state`,
   `consent_at`, `consent_proof_id`, subject to the two guards below.

### 5.1 Suppression guard (must not resurrect an unsubscriber)

`contact_profile.sendable` is `GENERATED ALWAYS AS (consent_state = 1 AND suppressed_at IS
NULL)`. Nothing today writes `contact_profile.consent_state` or `suppressed_at` on
unsubscribe — `unsub_server/db.py:93-127` only inserts into `suppressions` and sets
`recipients.status = 'suppressed'`. A blind `consent_state = 1` upsert therefore makes an
unsubscribed contact `sendable` again. The send itself would still be blocked by
`send_worker.py:491-499`, but the contact re-enters segments, audiences, and reporting.

The upsert must:

- set `suppressed_at = now()` and `consent_state = 2` when a `suppressions` row exists for
  `(tenant, email)` or `recipients.status = 'suppressed'`;
- never lower an existing `suppressed_at` to `NULL`;
- never move an existing `consent_state = 2` back to `1`.

### 5.2 Consent monotonicity

`consent_proof` rows are append-only evidence. `contact_profile.consent_proof_id` and
`consent_at` are updated **only when the incoming proof is newer**:

```sql
ON CONFLICT (tenant, contact_id, channel) DO UPDATE SET
  consent_proof_id = EXCLUDED.consent_proof_id,
  consent_at       = EXCLUDED.consent_at
WHERE contact_profile.consent_at IS NULL
   OR EXCLUDED.consent_at > contact_profile.consent_at
```

Without this, re-running a stale December export during a backfill regresses a January
consent timestamp.

### 5.3 Domain classification

`classify_domain_two_axis()` (`app/domain/isp.py:163`) is a pure seed-table lookup and does
not read `domain_provider`. Classification order:

1. `SELECT mailbox_provider, reputation_group FROM domain_provider WHERE tenant = :tenant AND domain = :domain`;
2. fall back to `classify_domain_two_axis(domain)`;
3. if that yields `('other', 'other')`, add the domain to the batch's unknown set.

After each batch, `enqueue_mx_lookup()` is called once per distinct unknown domain, bounded
by an `asyncio.Semaphore(50)`, and the resulting tasks are collected and awaited with
`asyncio.gather(..., return_exceptions=True)` before the CLI exits. `enqueue_mx_lookup`
(`domain_provider_mx_worker.py:79-116`) is fire-and-forget by design for a long-lived worker
process; in a CLI that calls `asyncio.run()`, un-awaited tasks are cancelled at loop close
and the classification is silently lost.

**Known gap, out of scope:** nothing propagates a later `domain_provider` correction back
onto existing `contact_profile` rows. A contact loaded against an unknown domain keeps
`('other', 'other')` even after MX resolves it. SPEC-segmentation §12 case 12 promises
reclassification within 24h; delivering it needs a sync pass in `profile_refresh`. Recorded
in §9 as a follow-up, not built here.

## 6. Quarantine and rejects

Two distinct rejection channels:

| Condition | Destination | `recipients` row |
|-----------|-------------|------------------|
| Missing or invalid email, unparseable row, decode error | `rejects.csv` | no |
| Parses, has email, but no TCPA text or no consent timestamp | `contact_quarantine` (`reason`, `raw` = full original row, `contact_id` NULL) **and** `rejects.csv` | no |

Consent captured later and re-submitted creates the contact normally; the quarantine row is
not promoted in place (SPEC-segmentation case 34).

`rejects.csv` carries PII and TCPA text. `--errors-out` defaults to
`import-logs/<timestamp>-rejects.csv`; `import-logs/` is added to `.gitignore`. Operators
are responsible for deleting it under the same retention discipline as `consent_proof`.

## 7. Idempotency and re-runs

There is no resume ledger. A failed run is recovered by re-running the whole file:

- `recipients` and `contact_profile` are idempotent upserts;
- `consent_proof` is deduped on `(tenant, contact_id, disclosure_text_hash, captured_at)`.

**This requires one new migration** — a unique index:

```sql
CREATE UNIQUE INDEX uq_consent_proof_dedup
  ON consent_proof (tenant, contact_id, disclosure_text_hash, captured_at);
```

Genuinely distinct consent events differ in `captured_at`, so the append-only evidence model
survives; only literal re-imports of the same event collapse.

## 8. `attributes.*` in the segment compiler

`app/services/segment_query.py` compiles `segments/email/*.yaml` predicates against
`contact_profile` and whitelists its columns in `_CP_FIELDS` (line 71). There is no
`attributes.*` path, so OTTO vehicle data is unreachable from the real segment engine —
`app/domain/segment.py:397` handles `attributes.` only for the legacy in-Python
`Workflow.entry_segment` matcher.

`_col()` gains an `attributes.<key>` branch that compiles to a correlated scalar subquery, so
no caller has to change its FROM clause:

```sql
(SELECT r.attributes -> :k FROM recipients r
  WHERE r.id = cp.contact_id AND r.tenant = cp.tenant)
```

- The key is validated against `^[a-z0-9_]{1,64}$` and passed as a bind parameter, never
  interpolated.
- Text operators use `->> :k`; numeric operators (`gt`, `gte`, `lt`, `lte`, `between`) cast
  `(... ->> :k)::numeric`.
- `is_null` / `is_not_null` test the JSON value for `NULL`, which also covers a missing key.

A new `contains` operator handles list-valued attributes such as `vehicle_makes`. The
existing `in` operator is scalar-only in both engines — `app/domain/segment.py:479` evaluates
`actual in clause["in"]`, which compares the whole list object against each candidate and
never matches. `contains` compiles to `jsonb_exists(<attr expr>, :p)` (function form, not the
`?` operator, which collides with driver placeholders) and evaluates in Python as membership
within a list-valued `actual`. `normalize()` accepts the new operator.

Performance: the correlated subquery is a PK lookup per candidate row. If the nightly
materialize slows measurably, hoist it to a `LEFT JOIN recipients` in the caller; that
changes `compile_segment`'s contract and is deliberately deferred.

## 9. Provider code map correction

`app/services/segment_query.py:42-60` disagrees with `app/domain/isp.py:23-41` and the
`domain_provider` seed in `alembic/versions/2026_07_30_1500-seg1_domain_provider.py:34-46`:

| provider | isp.py / DB seed | segment_query.py |
|----------|------------------|------------------|
| cable    | 4                | 5                |
| apple    | 5                | 4                |
| other    | 0                | 6                |
| corporate| 6                | absent           |
| microsoft| 2                | keyed `outlook`  |

`segments/email/cable_dormant.yaml:7` therefore compiles `mailbox_provider = 5` and selects
apple contacts. This design corrects `segment_query.py` to `isp.py`'s values, keeps
`outlook` as an alias for `microsoft`, adds `corporate`, drops `unknown`, updates the seed
codes in `tests/workers/test_segment_materialize.py:38`, and adds a test asserting the two
maps agree.

## 10. CLI

```bash
uv run python -m app.cli.load_records \
  --tenant otto --file leads.csv --vertical auto \
  [--batch-size 1000] [--errors-out PATH] [--limit N] \
  [--max-error-rate 0.25] [--enroll [--yes]] [--dry-run]
```

- Streams CSV or JSONL, chosen by extension; the whole file is never held in memory.
- Files are opened `newline=""`, `encoding="utf-8-sig"` — vendor CSVs are routinely
  UTF-8-with-BOM or cp1252, and `tcpa_text` can contain embedded newlines. A decode error
  rejects the row, it does not kill the run.
- Batches of `--batch-size` rows in one transaction, executed as per-row upserts rather than
  one multi-VALUES statement: two rows with the same email in one batch would otherwise
  raise `ON CONFLICT DO UPDATE command cannot affect row a second time`.
- A batch that raises does `await session.rollback()` and then retries its rows one at a
  time, each in its own transaction. Without the rollback, async SQLAlchemy fails every
  subsequent statement with `PendingRollbackError`.
- One structlog line per batch: `ingest.batch_complete` with `batch_start`, `read`, `loaded`,
  `updated`, `quarantined`, `rejected`, `enrolled`, `elapsed_ms`. A multi-hour run must not
  be silent until the end.
- End-of-run summary with the same counters.
- Exit 0 normally; exit 2 when the reject rate exceeds `--max-error-rate`.
- `--dry-run` validates, writes `rejects.csv`, and touches no DB — including no
  `domain_provider` placeholder inserts and no MX enqueue.
- `--enroll` prints `N contacts will be evaluated against M active workflows` and requires
  `--yes` above 1,000 projected enrollments; otherwise it aborts.
- `main()` disposes the engine in a `finally` block; `app/core/db.py` builds the engine at
  import time with default pool settings and a CLI must not leak connections at exit.

## 11. Endpoint (phase 2)

`POST /api/ingest/records/batch`, mounted under the `/api` prefix with
`Depends(require_api_key)` alongside the other authenticated routers in `app/main.py:45-49`.
Not under the bare `/ingest` prefix — that is `origin_platform`'s deliberately
unauthenticated private-network route (`app/main.py:44`), and this endpoint carries PII and
consent evidence.

Request: `{tenant, vertical, enroll: false, records: [...]}`, **max 200 records** per
request. Three writes per record puts 1000 records past typical gateway timeouts.
Response: per-row `{index, status: created|updated|quarantined|rejected, errors}`.
No new logic — it calls `record_ingest`. Enrollment via the endpoint is capped by the same
guard as the CLI.

## 12. Testing

Pure unit tests, no DB:

- normalizers: 7-digit / extension / international / blank phone; ZIP+4 and leading-zero ZIP;
  full state names vs abbreviations; year as string, blank, and `"N/A"`; name casing.
- mapping: indexed CSV vehicle columns and JSONL arrays; more than `max` vehicles; missing
  optional fields; derived fields including the empty-vehicle case.
- consent: hash stability under whitespace differences; missing text and missing timestamp
  both produce quarantine outcomes.
- segment DSL: `contains` against list and scalar values; `attributes.*` key validation.

Against the docker test DB:

- happy path writes all three rows with the expected codes;
- missing consent writes a `contact_quarantine` row and nothing else;
- re-import is idempotent for `recipients` and `contact_profile` and inserts no duplicate
  `consent_proof`;
- a suppressed contact stays `sendable = false` after re-import;
- a stale consent timestamp does not regress `contact_profile.consent_at`;
- an unknown domain inserts a `domain_provider` placeholder and the MX tasks are awaited;
- duplicate emails within one batch both apply, last one wins;
- `compile_segment` on `attributes.vehicle_count` and `attributes.vehicle_makes` returns the
  expected rows;
- the two provider code maps agree.

CLI end-to-end on a temporary CSV, including `--dry-run` writing nothing.

## 13. Out of scope, recorded

1. **`domain_provider` → `contact_profile` reclassification sync.** Required by
   SPEC-segmentation §12 case 12; nothing implements it. Needs a pass in `profile_refresh`.
2. **`app/api/workflows.py:242` is broken.** It calls
   `compile_segment(payload.segment, now=now)` against a signature that requires an
   `EngagementConfig` and returns a `(sql, params)` tuple, then passes the result to
   `.where()`. The workflow segment-preview endpoint raises `TypeError` at runtime.
3. **SMS consent.** `channels: [1]` only. A `[1, 2]` proof is defensible only if the
   disclosure text covers both (SPEC-segmentation §10).
4. **Hoisting the `attributes.*` subquery to a JOIN** if nightly materialize latency
   regresses.
