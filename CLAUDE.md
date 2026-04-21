# CLAUDE.md

Working instructions for Claude Code on this repo. Read this before doing anything.

## What this is

Standalone Email Service Provider for OTTO auto insurance remarketing. Replaces ExpertSender. Full product spec is in `SPEC.md` — **read it before starting work**.

## Working principles

1. **Build in milestones (see SPEC §17).** Each milestone is shippable. Stop at the end of each milestone and check in with the human before starting the next.

2. **Postgres only.** Do not introduce Redis, Kafka, SQS, ClickHouse, or any other infra without asking. The user explicitly chose Postgres-only for v1.

3. **Match Gauntlet patterns.** This codebase should feel like Gauntlet's sibling: Python 3.12, FastAPI, APScheduler, Aurora Postgres, Pydantic settings, structlog. If you're unsure how Gauntlet does X, ask.

4. **Domain logic is pure.** `app/domain/` modules must not import FastAPI or SQLAlchemy. They take primitives or dataclasses and return primitives or dataclasses. This makes them unit-testable without a DB.

5. **ESP adapter abstraction is sacred.** Even though v1 is SES-only, every send path goes through `ESPAdapter`. Never call boto3 SES directly outside `app/adapters/esp/ses.py`.

6. **Compliance is non-negotiable.** Every send must check suppression at the moment of send (not at queue time). Every email must have an unsub link, List-Unsubscribe header, and List-Unsubscribe-Post header.

7. **Use UUID v7 for all primary keys.** They're sortable and play nicely with Postgres b-tree indexes.

## What to do when uncertain

- Ambiguous requirement → ask.
- Want to pull in a new dependency → ask.
- Want to add a new table or migration → ask.
- Want to change something documented in SPEC.md → ask, and update SPEC.md as part of the change.
- Want to add a new node type to the workflow engine → ask.

## Commands

(Fill in as the project develops.)

```bash
# Install
uv sync

# Run API
uv run uvicorn app.main:app --reload

# Run workers
uv run python -m app.workers.scheduler

# DB migrations
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "description"

# Tests
uv run pytest
uv run pytest tests/integration -v
```

## Things that will trip you up

- **SES sandbox vs production.** New SES accounts start in sandbox (200/day, only verified addresses). Production access must be requested separately. Build assumes production access; in dev, point at the sandbox and use verified test addresses.
- **SES messageId vs SNS messageId.** They are different. Webhook joining uses SES messageId from inside the SNS payload, not the SNS envelope's messageId.
- **List-Unsubscribe-Post headers.** Gmail and Yahoo require RFC 8058 one-click unsub for senders > 5K/day. The header alone is not enough — the POST endpoint must actually work without auth and must unsubscribe immediately.
- **Materialized view refresh.** `REFRESH MATERIALIZED VIEW CONCURRENTLY` requires a unique index. The spec includes one — don't drop it.
- **Workflow versioning.** `journeys.workflow_version` pins the version at entry. If a workflow is edited mid-flight for some recipients, those recipients keep using the old version. Don't try to migrate them.
- **Idempotency on ingest.** Recipients ingestion is idempotent on email. Don't create duplicate journeys if the same lead is re-ingested while already in a workflow.
