# Standalone Unsubscribe Server — Design

Date: 2026-07-28
Status: approved (design), not yet implemented

## Purpose

A minimal, independently deployable unsubscribe service that any number of
sending domains (`unsub.karzinsurance.com`, `unsub.<future-domain>`, ...) can
point at via DNS. It exists so unsubscribe handling can run on
single-purpose public servers without shipping the cadence codebase to them.

## Decisions made

- **Data path:** direct Postgres writes to the shared Aurora DB. No cadence
  API dependency; an unsub lands durably even if the cadence API is down.
- **Repo layout:** same repo, own top-level directory `unsub_server/` with
  its own `pyproject.toml`, `uv.lock`, tests, and Dockerfile. Zero imports
  from `app/`. The deploy artifact is that folder only.
- **Multi-domain:** one server for all domains. The signed token already
  carries tenant + recipient id, so the Host header is irrelevant to
  correctness. Adding a domain = one DNS record + TLS cert on the fronting
  LB/CDN; no server change.

## Architecture

```
mailbox provider / recipient browser
        │  GET|POST https://unsub.<domain>/u/{token}
        ▼
  unsub_server (FastAPI, ~3 routes, own container)
        │  asyncpg/SQLAlchemy-core, dedicated DB role
        ▼
  Aurora Postgres (shared with cadence)
```

Components:

- `unsub_server/main.py` — FastAPI app, three routes (below).
- `unsub_server/tokens.py` — token verification: itsdangerous
  `URLSafeTimedSerializer`, salt `cadence-unsub-v1`, max age 180 days.
  Deliberate copy of the verify half of `app/core/unsub.py`.
- `unsub_server/db.py` — raw SQL via SQLAlchemy core on the asyncpg
  driver (same engine stack as cadence, but no ORM models and no alembic;
  this service never migrates anything).

## Endpoints

Behavior mirrors cadence's existing `app/api/unsub.py` exactly:

- `GET /u/{token}` — verify token, look up recipient, render the HTML
  confirm page (form POSTs to the same URL). Invalid/expired token → 410.
- `POST /u/{token}` — RFC 8058 one-click. No auth. Verify token, then in
  one transaction:
  1. `INSERT INTO suppressions (tenant, email, reason='unsubscribe',
     source='one_click')` — idempotent on `(tenant, email)` unique
     constraint (conflict → treat as already unsubscribed, continue).
  2. `INSERT INTO unsub_outbox (tenant, email, reason, occurred_at)` —
     guarded: skip if an undelivered row for the same tenant+email exists
     from the last hour (matches cadence's spam guard).
  3. `UPDATE recipients SET status='suppressed' WHERE id = ...`.
  Returns the "Unsubscribed" HTML page (200). Bad token → 410; unknown
  recipient → 404.
- `GET /health` — DB ping, for the LB.

## Configuration (env only, no secrets in code or DB)

- `UNSUB_DATABASE_URL` — dedicated DB role, least privilege:
  `SELECT recipients`, `UPDATE recipients (status)`,
  `INSERT suppressions`, `SELECT/INSERT unsub_outbox`.
- `CADENCE_UNSUB_SIGNING_KEY` — same value as the cadence deployment;
  tokens must verify across both.

## Shared-contract protection

Two things must stay in sync with cadence: token format and the shapes of
`suppressions` / `unsub_outbox` / `recipients`. Protection is
duplication-plus-CI, not shared imports:

- A cross-check test in **cadence's** suite signs a token with
  `app/core/unsub.py` and verifies it with `unsub_server/tokens.py`
  (and vice versa). Salt or serialization drift breaks CI.
- `unsub_server`'s own test suite runs its routes against the same test DB
  the cadence suite uses, so schema drift breaks CI too.

## Cadence-side changes

None required. Unsub links already point at `unsub.<sending_domain>`
(per-identity hosts, commit 6ebfe93). Cadence keeps its own `/u/` routes —
harmless duplication that keeps local dev working with one process.

## Error handling

- Invalid/expired token: 410 on both routes (matches cadence today).
- DB unavailable: 500; the mailbox provider retries one-click POSTs.
  No local queue in v1 — Postgres (Aurora) is the availability story.
- Duplicate unsub: success response, no duplicate rows (unique constraint
  + outbox guard).

## Testing

- Unit: token verify (valid, expired, garbage), HTML escaping.
- Integration (against the test DB): POST inserts suppression + outbox +
  flips recipient status; idempotent second POST; outbox hour-guard;
  GET renders confirm page; 410/404 paths.
- Cross-check contract tests as above.

## Out of scope (v1)

- TLS/cert management for the many domains (belongs to the fronting
  ALB/CloudFront config, not this repo).
- Per-domain signing keys (single shared key; revisit if a tenant ever
  needs key isolation).
- Static-page split (the confirm page is served by this same service;
  it is already the single-purpose box the static idea was reaching for).
- List-management preferences UI. Unsubscribe is total per tenant.
