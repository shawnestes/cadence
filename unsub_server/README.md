# cadence unsub server

Standalone unsubscribe service. Any sending domain's `unsub.<domain>` DNS
points here; the signed token identifies tenant + recipient, so one
deployment serves every domain.

## Run

    docker build -f unsub_server/Dockerfile -t cadence-unsub .   # from repo root
    docker run -p 8080:8080 \
      -e UNSUB_DATABASE_URL=postgresql+asyncpg://unsub_server:...@host/db \
      -e CADENCE_UNSUB_SIGNING_KEY=... \
      cadence-unsub

`CADENCE_UNSUB_SIGNING_KEY` must equal the cadence deployment's key.
`UNSUB_DATABASE_URL` should use the least-privilege `unsub_server` role
(see `unsub_server/sql/create_role.sql`).

## Routes

- `GET /u/{token}` — confirmation page
- `POST /u/{token}` — RFC 8058 one-click unsubscribe (no auth)
- `GET /health` — DB ping

Tests live in the cadence suite (`tests/test_unsub_server_*.py`) and run
with `uv run pytest` from the repo root.

## Reproducible builds

`unsub_server/uv.lock` pins every dependency; regenerate it with
`uv lock --project unsub_server` from the repo root whenever
`unsub_server/pyproject.toml` changes, and commit the updated lock.
The Dockerfile installs from that lock with `uv sync --frozen --no-dev`.

To sanity-check the image without a DB:

    docker run --rm cadence-unsub uv run --project unsub_server --frozen --no-dev \
      python -c "import unsub_server.main; print('app ok')"
