# Sending records to Cadence

Two front doors, one pipeline: the CLI (`app.cli.load_records`) for files, and
`POST /api/ingest/records/batch` for HTTP. Both map a raw record through the
tenant's YAML mapping (`config/tenants/<tenant>.yaml`) and write the same three
tables — `recipients`, `consent_proof`, `contact_profile`.

Everything below is for tenant `otto`. A different tenant means a different
YAML mapping and therefore different column names.

---

## 1. The rules that decide what happens to a record

Each record ends in exactly one of four states.

| Status | Meaning | Writes a recipient? |
|---|---|---|
| `created` | New contact loaded | yes |
| `updated` | Existing contact re-loaded | yes (upsert) |
| `quarantined` | Valid record, but **no usable consent** | **no** — `contact_quarantine` only |
| `rejected` | Record could not be mapped at all | no |

Non-negotiables baked into the pipeline:

- **No consent, no contact.** Missing `tcpa_text` or an unparseable
  `tcpa_date` quarantines the record. No recipient row is created.
- **A re-import never resurrects an unsubscriber.** `consent_state` only
  ratchets toward opted-out and `suppressed_at` is never cleared.
- **`consent_at` moves forward only.** Replaying a stale export cannot regress
  a newer consent proof.
- **`attributes` is replaced wholesale, not merged.** Each record is a complete
  snapshot — an omitted field becomes explicit `null`, it does not keep its old
  value. Send every field you want retained, every time.
- **Re-running the same file is safe.** A unique index dedupes consent proofs.

---

## 2. CSV format

UTF-8 (with or without BOM) or cp1252. Encoding is auto-detected by probing the
first 1 MiB; override with `--encoding` if you know better. Header row required.
Column order does not matter. Extra columns are ignored.

### Full header row

```csv
email,vertical,user_id,first_name,last_name,city,state,zip,phone,year,make,model,vin,current_company,jornaya_id,trusted_form_cert,tcpa_text,tcpa_date,consent_url,lead_id,ip,user_agent
```

### Example row

```csv
shawn@example.com,auto,otto-99182,SHAWN,ESTES,Austin,texas,7870,5125551234,2019,Toyota,Camry,4T1BF1FK5CU511111,Allstate,2EAB0F47-BA9A-3840-4593-8EC5D6E057BB,https://cert.trustedform.com/abc123,"By submitting you agree to receive emails.",2026-01-15T10:30:00Z,https://otto.example/quote,form-7,203.0.113.9,Mozilla/5.0
```

That row loads as `first_name=Shawn`, `state=TX`, `zip=07870`, `phone=+15125551234`.

### Column reference

| Column | Required | Normalized to | Bad value means |
|---|---|---|---|
| `email` | **yes** | lowercased, trimmed | record **rejected** |
| `vertical` | no | trimmed; falls back to `--vertical` | uses `--vertical` |
| `user_id` | no | trimmed | stored as-is |
| `first_name`, `last_name` | no | whitespace collapsed; ALL-CAPS or all-lower title-cased, mixed case left alone | `null` |
| `city` | no | trimmed | `null` |
| `state` | no | 2-letter USPS code (`texas` → `TX`) | `null` |
| `zip` | no | 5 digits, leading zeros restored; ZIP+4 truncated | `null` |
| `phone` | no | `+1XXXXXXXXXX` | `null` |
| `current_company` | no | trimmed | `null` |
| `jornaya_id` | no | trimmed | `null` |
| `trusted_form_cert` | no | trimmed | `null` |
| `tcpa_text` | **yes** | trimmed | record **quarantined** |
| `tcpa_date` | **yes** | ISO-8601; naive treated as UTC | record **quarantined** |
| `consent_url` | no | trimmed | `null` |
| `lead_id` | no | trimmed | `null` |
| `ip` | no | canonical IPv4/IPv6 | `null` (never fails the record) |
| `user_agent` | no | trimmed | `null` |

**Column name vs attribute name.** Segments read `attributes.<key>`; every
column above keeps its own name as the attribute key (`user_id` included).
`attributes.address1` exists but is always `null` — OTTO's export carries no
mailing address column.

**Lead certification.** `jornaya_id` and `trusted_form_cert` are third-party
evidence of the consent event. They are optional (an older export may omit
them, and a missing cert must never reject a record), and they are written to
both `attributes` and `consent_proof.raw.attributes` on every proof row.

Notes on the fussy ones:

- **`state`** accepts the abbreviation or the full name. Anything else → `null`.
- **`zip`** accepts `78701`, `78701-1234`, `787011234`, and spreadsheet-mangled
  `1234` (→ `01234`). 1–2 digits or 6–8 digits → `null`.
- **`phone`** is US-only: 10 digits, or 11 leading with `1`. 7-digit local
  numbers, extensions, and international numbers are rejected to `null` rather
  than guessed at.
- **`tcpa_date`** accepts `2026-01-15T10:30:00Z`, `+00:00` offsets, and
  naive `2026-01-15 10:30:00` (treated as UTC). Anything `datetime.fromisoformat`
  won't parse quarantines the record.
- **`ip`** is corroborating evidence, not the legally meaningful part of
  the proof — a malformed IP is dropped to `null` and the record still loads.
  Zone-scoped IPv6 (`fe80::1%eth0`) is treated as invalid; Postgres `inet`
  rejects it.

### Vehicle (repeating group, max 1)

Flat columns named for the field itself: `year`, `make`, `model`, `vin`.
Include only the columns you have — an entirely empty set is skipped, not
stored as a blank vehicle.

```csv
year,make,model
2019,Toyota,Camry
```

**One vehicle per record.** The mapping sets `max: 1` because flat column names
carry no index — there is no way to express a second vehicle in CSV. Multiple
vehicles require the JSONL `vehicles` array below, and even then only the first
is kept while `max: 1` stands.

`year` must be a whole calendar year between 1900 and 2100. `2019.7`, `inf`,
and `1e400` are dropped, not truncated.

Three attributes are derived automatically and are queryable from segments:

- `vehicle_count` — integer
- `vehicle_makes` — lowercased, de-duplicated list (`["toyota"]`)
- `newest_vehicle_year` — integer

### JSONL

Same field names, one JSON object per line. Use `.jsonl` or `.ndjson`. Vehicles
may be a real array under the `vehicles` key instead of the flat columns:

```json
{"email":"shawn@example.com","user_id":"otto-99182","first_name":"Shawn","state":"TX","zip":"78701","phone":"5125551234","vehicles":[{"year":2019,"make":"Toyota","model":"Camry"}],"jornaya_id":"2EAB0F47-BA9A-3840-4593-8EC5D6E057BB","trusted_form_cert":"https://cert.trustedform.com/abc123","tcpa_text":"By submitting you agree to receive emails.","tcpa_date":"2026-01-15T10:30:00Z","ip":"203.0.113.9"}
```

A `vehicles` value that is a JSON-*encoded string* rather than a real array
yields `[]` silently. Send an actual array.

---

## 3. Loading a file (CLI)

```bash
# validate only — writes the rejects file, touches the DB in no way
uv run python -m app.cli.load_records \
  --tenant otto --file leads.csv --vertical auto --dry-run

# real load
uv run python -m app.cli.load_records \
  --tenant otto --file leads.csv --vertical auto
```

Always dry-run an unfamiliar vendor file first and read the rejects CSV.

### Flags

| Flag | Default | What it does |
|---|---|---|
| `--tenant` | required | picks `config/tenants/<tenant>.yaml` |
| `--file` | required | `.csv`, `.jsonl`, or `.ndjson` |
| `--vertical` | required | `auto` or `home` |
| `--dry-run` | off | validate and report; no DB writes |
| `--batch-size` | 1000 | records per transaction |
| `--encoding` | `auto` | e.g. `cp1252` to skip probing |
| `--errors-out` | `import-logs/<ts>-rejects.csv` | rejects file |
| `--limit` | 0 (all) | stop after N records |
| `--max-error-rate` | 0.25 | exit 2 if (quarantined+rejected)/read exceeds this |
| `--enroll` | off | also enroll loaded contacts into matching active workflows |
| `--yes` | off | required alongside `--enroll` |

### Output

A summary line, one `key=value` per counter:

```
read=15000 loaded=14210 updated=505 quarantined=190 rejected=95 enrolled=0
```

`loaded + updated + quarantined + rejected == read`, always. If it doesn't,
that's a bug — report it.

Exit codes: `0` success, `2` bad arguments / missing file / undecodable file /
error rate above `--max-error-rate`.

### The rejects file

CSV with `row_number,outcome,errors,raw`. One row per record that did not load.
`outcome` is `quarantine`, `reject`, or `error` (an unexpected failure at the
database level).

**It contains PII and full consent text.** `import-logs/` is gitignored. Treat
the file as you would the source export.

---

## 4. Sending records over HTTP

```
POST /api/ingest/records/batch
X-API-Key: <key>
Content-Type: application/json
```

Same pipeline, same rules. **Max 200 records per request** — three DB writes per
record, and 200 keeps the request inside gateway timeouts. 201+ records is a 422.

### Request

```json
{
  "tenant": "otto",
  "vertical": "auto",
  "enroll": false,
  "records": [
    {
      "email": "shawn@example.com",
      "user_id": "otto-99182",
      "first_name": "Shawn",
      "last_name": "Estes",
      "city": "Austin",
      "state": "TX",
      "zip": "78701",
      "phone": "5125551234",
      "vehicles": [{"year": 2019, "make": "Toyota", "model": "Camry"}],
      "tcpa_text": "By submitting you agree to receive emails.",
      "tcpa_date": "2026-01-15T10:30:00Z",
      "consent_url": "https://otto.example/quote",
      "lead_id": "form-7",
      "ip": "203.0.113.9",
      "user_agent": "Mozilla/5.0"
    }
  ]
}
```

Each object in `records` uses the **same field names as the CSV columns** —
flat `year`/`make`/`model` keys work too, but a real `vehicles` array is
cleaner over JSON.

- `tenant` defaults to `"otto"`.
- `vertical` is required: `"auto"` or `"home"`.
- `enroll` must be `false` or omitted. `true` returns **400** — enrollment over
  HTTP is not implemented yet; use the CLI's `--enroll`.

### Response — always 200 if the request itself was well-formed

```json
{
  "results": [
    {"index": 0, "status": "created", "errors": []},
    {"index": 1, "status": "quarantined",
     "errors": ["missing consent text (column 'tcpa_text')"]},
    {"index": 2, "status": "rejected",
     "errors": ["invalid or missing email in column 'email'"]},
    {"index": 3, "status": "error", "errors": ["..."]}
  ],
  "counters": {"read": 4, "loaded": 1, "updated": 0,
               "quarantined": 1, "rejected": 2, "enrolled": 0}
}
```

`results[i].index` maps back to the position in your `records` array. **Check
every result** — a 200 does not mean every record loaded.

`status: "error"` means that record hit an unexpected database-level failure.
It is isolated by a SAVEPOINT, so its siblings in the same request still commit.
Retry those records; do not retry the whole batch blindly.

In `counters`, `"error"` records are folded into `rejected`.

### Status codes

| Code | Cause |
|---|---|
| 200 | Request processed — read `results` for per-record outcomes |
| 400 | `enroll: true` |
| 401 | Missing or wrong `X-API-Key` |
| 422 | Malformed body, unknown `vertical`, or more than 200 records |
| 500 | The final commit failed — **nothing in the batch was written**, retry it |

A 500 is the one case where per-record statuses are moot: all records share a
single final commit, so if that commit fails, none of them landed.

### curl

```bash
curl -sS -X POST https://<host>/api/ingest/records/batch \
  -H "X-API-Key: $CADENCE_API_KEY" \
  -H 'Content-Type: application/json' \
  --data @batch.json
```

### Choosing a front door

Use the **CLI** for vendor file drops, backfills, and anything over a few
thousand records — it streams, batches, retries per record, and writes a rejects
file. Use the **endpoint** for live/near-real-time delivery from an upstream
system, chunked into requests of 200.

---

## 5. Troubleshooting

| Symptom | Cause |
|---|---|
| Everything `rejected`, "invalid or missing email" | wrong column name, or CSV delimiter isn't a comma |
| Everything `quarantined` | `tcpa_text` / `tcpa_date` column missing or empty — no consent, no contact |
| Names arrive mojibake'd (`JosÃ©`) | encoding probe guessed wrong; re-run with `--encoding cp1252` or `--encoding utf-8` |
| CLI exits 2 with "error rate ... exceeds" | more than 25% of records failed; inspect the rejects file, raise `--max-error-rate` only once you know why |
| `--enroll` refuses to run | add `--yes` |
| Fields you didn't send came back `null` | expected — `attributes` is a full snapshot, not a merge |
| Record loaded but contact still suppressed | expected — a re-import never clears `suppressed_at` |
| HTTP 422 on a large payload | more than 200 records; chunk it |

Related: `SPEC-segmentation.md` §9 (consent), `config/tenants/otto.yaml` (the
mapping itself).
