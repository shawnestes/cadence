# Standalone Unsub Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-contained `unsub_server/` FastAPI micro-service (zero imports from `app/`) that serves `GET|POST /u/{token}` for any `unsub.<domain>` host and writes unsubscribes directly to the shared Postgres.

**Architecture:** Three modules — `tokens.py` (verify-only copy of cadence's signer), `db.py` (raw SQL over SQLAlchemy core + asyncpg), `main.py` (FastAPI, 3 routes). Contract drift with cadence is caught by cross-check tests that live in **cadence's** pytest suite (repo root is importable there). Deployment artifact is the folder + its own pyproject + Dockerfile.

**Tech Stack:** Python 3.12, FastAPI, itsdangerous, SQLAlchemy (core only) + asyncpg, uuid-utils, uv.

**Spec:** `docs/superpowers/specs/2026-07-28-unsub-server-design.md` — read it first.

## Global Constraints

- `unsub_server/` must never import from `app/` (spec: "zero imports from app/"). Cadence tests MAY import `unsub_server` (cross-checks).
- Token contract, copied verbatim from `app/core/unsub.py`: `URLSafeTimedSerializer(signing_key, salt="cadence-unsub-v1")`, payload dict keys `{"r": recipient_id, "s": send_id, "t": tenant}`, max age `60*60*24*180` seconds.
- Env vars only: `UNSUB_DATABASE_URL`, `CADENCE_UNSUB_SIGNING_KEY`. Never hardcode or log secrets.
- All new primary keys are UUID v7 (`uuid_utils.uuid7`, converted to stdlib `uuid.UUID`).
- Response behavior must mirror `app/api/unsub.py`: bad/expired token → 410 on GET and POST; recipient missing → 410 on GET, 404 on POST; successful POST → 200 HTML "Unsubscribed" page.
- Cadence test suite conventions: async tests need no marker (`asyncio_mode = "auto"`); HTTP tests use `httpx.AsyncClient` over `ASGITransport`, never FastAPI's sync `TestClient`; DB fixtures come from `tests/conftest.py` (`db_session`).
- Tests run against the DB configured for the cadence suite (docker `docker-compose.test.yml` or `CADENCE_TEST_DATABASE_URL`). Run `uv run pytest` from the repo root.
- Commit messages: imperative, conventional prefixes, NO Co-Authored-By / attribution lines.

---

### Task 1: Package skeleton + token verification with cross-check tests

**Files:**
- Create: `unsub_server/__init__.py` (empty)
- Create: `unsub_server/tokens.py`
- Test: `tests/test_unsub_server_tokens.py` (in cadence's suite)

**Interfaces:**
- Produces: `unsub_server.tokens.UnsubPayload` (dataclass: `recipient_id: str`, `send_id: str | None`, `tenant: str`), `unsub_server.tokens.UnsubTokenError(Exception)`, `unsub_server.tokens.verify(token: str, signing_key: str) -> UnsubPayload`. Task 3 calls `verify()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_unsub_server_tokens.py
"""Cross-check: tokens signed by cadence verify in unsub_server, and drift
in salt/payload shape breaks this file."""

import pytest

from app.core.unsub import UnsubPayload as CadencePayload, UnsubSigner
from unsub_server.tokens import UnsubPayload, UnsubTokenError, verify

KEY = "cross-check-key"


def _cadence_token(recipient_id="rid-1", send_id="sid-1", tenant="otto") -> str:
    signer = UnsubSigner(signing_key=KEY, base_url="http://x")
    return signer.sign(CadencePayload(recipient_id=recipient_id, send_id=send_id, tenant=tenant))


def test_cadence_signed_token_verifies_in_unsub_server():
    payload = verify(_cadence_token(), signing_key=KEY)
    assert payload == UnsubPayload(recipient_id="rid-1", send_id="sid-1", tenant="otto")


def test_null_send_id_roundtrips():
    token = _cadence_token(send_id=None)
    assert verify(token, signing_key=KEY).send_id is None


def test_wrong_key_rejected():
    with pytest.raises(UnsubTokenError):
        verify(_cadence_token(), signing_key="other-key")


def test_garbage_rejected():
    with pytest.raises(UnsubTokenError):
        verify("not-a-token", signing_key=KEY)


def test_expired_rejected():
    with pytest.raises(UnsubTokenError):
        verify(_cadence_token(), signing_key=KEY, max_age=-1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_unsub_server_tokens.py -q`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'unsub_server'`

- [ ] **Step 3: Implement**

```python
# unsub_server/tokens.py
"""Verify-only copy of cadence's unsub token signer (app/core/unsub.py).

Deliberately duplicated so this service never imports cadence code; the
cross-check tests in tests/test_unsub_server_tokens.py keep the copies in
lockstep. Salt and payload keys MUST match cadence exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_SALT = "cadence-unsub-v1"
_MAX_AGE_SECONDS = 60 * 60 * 24 * 180  # 180 days


class UnsubTokenError(Exception):
    pass


@dataclass(slots=True)
class UnsubPayload:
    recipient_id: str
    send_id: str | None
    tenant: str


def verify(token: str, signing_key: str, max_age: int = _MAX_AGE_SECONDS) -> UnsubPayload:
    serializer = URLSafeTimedSerializer(signing_key, salt=_SALT)
    try:
        data = serializer.loads(token, max_age=max_age)
    except SignatureExpired as e:
        raise UnsubTokenError("token expired") from e
    except BadSignature as e:
        raise UnsubTokenError("bad signature") from e
    return UnsubPayload(recipient_id=data["r"], send_id=data.get("s"), tenant=data["t"])
```

Also create empty `unsub_server/__init__.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_unsub_server_tokens.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add unsub_server/__init__.py unsub_server/tokens.py tests/test_unsub_server_tokens.py
git commit -m "feat: unsub_server package with token verification + cadence cross-check tests"
```

---

### Task 2: Database layer (raw SQL, no ORM)

**Files:**
- Create: `unsub_server/db.py`
- Test: `tests/test_unsub_server_db.py` (in cadence's suite; uses cadence fixtures to seed rows)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces (Task 3 relies on these exact signatures):
  - `unsub_server.db.get_engine() -> AsyncEngine` — lazy singleton from env `UNSUB_DATABASE_URL`.
  - `unsub_server.db.reset_engine() -> None` — drops the singleton (tests).
  - `unsub_server.db.fetch_recipient(tenant: str, recipient_id: str) -> dict | None` — `{"id": UUID, "email": str, "status": str}` or None. Invalid UUID string → None.
  - `unsub_server.db.record_unsub(tenant: str, recipient_id: str, email: str) -> None` — one transaction: idempotent suppression insert, hour-guarded unsub_outbox insert, recipient status update.
  - `unsub_server.db.ping() -> bool` — `SELECT 1` health check.

- [ ] **Step 1: Write the failing tests**

Table shapes being written to (from cadence's models — do not change them):
`suppressions(id uuid PK, tenant text, email citext, reason text, source text, suppressed_at timestamptz DEFAULT now(), created_at/updated_at DEFAULT now(), UNIQUE(tenant,email))`;
`unsub_outbox(id uuid PK, tenant text, email citext, reason text, occurred_at timestamptz, attempts int DEFAULT 0, created_at/updated_at DEFAULT now())`;
`recipients(... status text ...)`.

```python
# tests/test_unsub_server_db.py
"""unsub_server.db against the cadence test database. Seeds rows with
cadence's ORM (test-side convenience only; the server itself is ORM-free)."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core.db import engine as cadence_engine
from app.models.recipient import Recipient
from app.models.suppression import Suppression
from app.models.unsub_outbox import UnsubOutbox
from unsub_server import db as udb


@pytest.fixture(autouse=True)
def _point_at_test_db(monkeypatch):
    url = cadence_engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("UNSUB_DATABASE_URL", url)
    udb.reset_engine()
    yield
    udb.reset_engine()


async def _seed_recipient(db_session, email: str) -> Recipient:
    now = datetime.now(timezone.utc)
    r = Recipient(
        tenant="otto",
        email=email,
        vertical="auto",
        first_seen_at=now,
        last_seen_at=now,
        consent_source="test",
        consent_at=now,
        attributes={},
    )
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)
    return r


async def test_fetch_recipient_found(db_session):
    r = await _seed_recipient(db_session, f"unsub-db-{uuid.uuid4()}@cadencetests.io")
    row = await udb.fetch_recipient("otto", str(r.id))
    assert row is not None
    assert row["email"] == r.email
    assert row["status"] == "active"


async def test_fetch_recipient_wrong_tenant_is_none(db_session):
    r = await _seed_recipient(db_session, f"unsub-db-{uuid.uuid4()}@cadencetests.io")
    assert await udb.fetch_recipient("not-otto", str(r.id)) is None


async def test_fetch_recipient_invalid_uuid_is_none(db_session):
    assert await udb.fetch_recipient("otto", "not-a-uuid") is None


async def test_record_unsub_writes_all_three(db_session):
    r = await _seed_recipient(db_session, f"unsub-db-{uuid.uuid4()}@cadencetests.io")

    await udb.record_unsub("otto", str(r.id), r.email)

    sup = (
        await db_session.execute(
            select(Suppression).where(Suppression.tenant == "otto", Suppression.email == r.email)
        )
    ).scalar_one()
    assert sup.reason == "unsubscribe"
    assert sup.source == "one_click"

    outbox = (
        await db_session.execute(
            select(UnsubOutbox).where(UnsubOutbox.tenant == "otto", UnsubOutbox.email == r.email)
        )
    ).scalars().all()
    assert len(outbox) == 1

    await db_session.refresh(r)
    assert r.status == "suppressed"


async def test_record_unsub_idempotent(db_session):
    r = await _seed_recipient(db_session, f"unsub-db-{uuid.uuid4()}@cadencetests.io")

    await udb.record_unsub("otto", str(r.id), r.email)
    await udb.record_unsub("otto", str(r.id), r.email)  # must not raise

    outbox = (
        await db_session.execute(
            select(UnsubOutbox).where(UnsubOutbox.tenant == "otto", UnsubOutbox.email == r.email)
        )
    ).scalars().all()
    assert len(outbox) == 1  # hour-guard: second call adds nothing


async def test_ping():
    assert await udb.ping() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_unsub_server_db.py -q`
Expected: collection ERROR — `ModuleNotFoundError`/`ImportError` for `unsub_server.db`

- [ ] **Step 3: Implement**

```python
# unsub_server/db.py
"""Raw-SQL data layer. SQLAlchemy core over asyncpg; no ORM, no alembic --
this service never owns or migrates schema. Column shapes are cadence's;
drift is caught by tests/test_unsub_server_db.py in cadence's suite."""

from __future__ import annotations

import os
import uuid

import uuid_utils
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_engine: AsyncEngine | None = None


def _uuid7() -> uuid.UUID:
    return uuid.UUID(bytes=uuid_utils.uuid7().bytes)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = os.environ["UNSUB_DATABASE_URL"]
        _engine = create_async_engine(url, pool_size=5, max_overflow=5)
    return _engine


def reset_engine() -> None:
    global _engine
    _engine = None


async def ping() -> bool:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def fetch_recipient(tenant: str, recipient_id: str) -> dict | None:
    try:
        rid = uuid.UUID(recipient_id)
    except ValueError:
        return None
    async with get_engine().connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, email, status FROM recipients "
                    "WHERE tenant = :t AND id = :id"
                ),
                {"t": tenant, "id": rid},
            )
        ).mappings().first()
    return dict(row) if row is not None else None


async def record_unsub(tenant: str, recipient_id: str, email: str) -> None:
    """Suppress + queue outbox + flip recipient status, atomically.

    Mirrors cadence's POST /u/{token} (app/api/unsub.py): suppression is
    idempotent on (tenant, email); the outbox insert is skipped when an
    undelivered row for the same tenant+email exists from the last hour.
    """
    rid = uuid.UUID(recipient_id)
    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO suppressions (id, tenant, email, reason, source) "
                "VALUES (:id, :t, :e, 'unsubscribe', 'one_click') "
                "ON CONFLICT (tenant, email) DO NOTHING"
            ),
            {"id": _uuid7(), "t": tenant, "e": email},
        )
        await conn.execute(
            text(
                "INSERT INTO unsub_outbox (id, tenant, email, reason, occurred_at) "
                "SELECT :id, :t, :e, 'unsubscribe', now() "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM unsub_outbox "
                "  WHERE tenant = :t AND email = :e "
                "    AND delivered_at IS NULL "
                "    AND created_at >= now() - interval '1 hour'"
                ")"
            ),
            {"id": _uuid7(), "t": tenant, "e": email},
        )
        await conn.execute(
            text("UPDATE recipients SET status = 'suppressed', updated_at = now() WHERE id = :id"),
            {"id": rid},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_unsub_server_db.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add unsub_server/db.py tests/test_unsub_server_db.py
git commit -m "feat: unsub_server raw-SQL data layer with schema cross-check tests"
```

---

### Task 3: FastAPI app (3 routes)

**Files:**
- Create: `unsub_server/main.py`
- Test: `tests/test_unsub_server_routes.py` (in cadence's suite)

**Interfaces:**
- Consumes: `unsub_server.tokens.verify/UnsubTokenError` (Task 1); `unsub_server.db.fetch_recipient/record_unsub/ping` (Task 2).
- Produces: `unsub_server.main.app` (FastAPI instance) — what uvicorn serves and what the Dockerfile (Task 4) targets as `unsub_server.main:app`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_unsub_server_routes.py
"""unsub_server HTTP behavior, exercised over ASGITransport against the
cadence test DB. Tokens are signed with cadence's signer -- full-loop
contract: cadence signs, unsub_server verifies and writes."""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.db import engine as cadence_engine
from app.core.unsub import UnsubPayload, UnsubSigner
from app.models.recipient import Recipient
from app.models.suppression import Suppression

KEY = "unsub-server-route-test-key"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from unsub_server import db as udb

    monkeypatch.setenv(
        "UNSUB_DATABASE_URL", cadence_engine.url.render_as_string(hide_password=False)
    )
    monkeypatch.setenv("CADENCE_UNSUB_SIGNING_KEY", KEY)
    udb.reset_engine()
    yield
    udb.reset_engine()


@pytest.fixture
async def unsub_client():
    from unsub_server.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://unsub.test") as c:
        yield c


async def _seed_recipient(db_session, email: str) -> Recipient:
    now = datetime.now(timezone.utc)
    r = Recipient(
        tenant="otto",
        email=email,
        vertical="auto",
        first_seen_at=now,
        last_seen_at=now,
        consent_source="test",
        consent_at=now,
        attributes={},
    )
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)
    return r


def _token(recipient_id: str) -> str:
    signer = UnsubSigner(signing_key=KEY, base_url="http://x")
    return signer.sign(UnsubPayload(recipient_id=recipient_id, send_id=None, tenant="otto"))


async def test_get_renders_confirm_page(unsub_client, db_session):
    r = await _seed_recipient(db_session, f"unsub-rt-{uuid.uuid4()}@cadencetests.io")
    resp = await unsub_client.get(f"/u/{_token(str(r.id))}")
    assert resp.status_code == 200
    assert r.email in resp.text
    assert "form" in resp.text and "POST" in resp.text.upper()


async def test_get_bad_token_410(unsub_client):
    resp = await unsub_client.get("/u/garbage")
    assert resp.status_code == 410


async def test_post_unsubscribes(unsub_client, db_session):
    r = await _seed_recipient(db_session, f"unsub-rt-{uuid.uuid4()}@cadencetests.io")
    resp = await unsub_client.post(f"/u/{_token(str(r.id))}")
    assert resp.status_code == 200
    assert "Unsubscribed" in resp.text

    sup = (
        await db_session.execute(
            select(Suppression).where(Suppression.tenant == "otto", Suppression.email == r.email)
        )
    ).scalar_one()
    assert sup.source == "one_click"
    await db_session.refresh(r)
    assert r.status == "suppressed"


async def test_post_bad_token_410(unsub_client):
    resp = await unsub_client.post("/u/garbage")
    assert resp.status_code == 410


async def test_post_unknown_recipient_404(unsub_client):
    resp = await unsub_client.post(f"/u/{_token(str(uuid.uuid4()))}")
    assert resp.status_code == 404


async def test_get_unknown_recipient_410(unsub_client):
    resp = await unsub_client.get(f"/u/{_token(str(uuid.uuid4()))}")
    assert resp.status_code == 410


async def test_health(unsub_client):
    resp = await unsub_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_unsub_server_routes.py -q`
Expected: ERRORS — `ModuleNotFoundError: No module named 'unsub_server.main'`

- [ ] **Step 3: Implement**

HTML/style constants are copied from `app/api/unsub.py` (keep the markup
identical so recipients see the same page either way).

```python
# unsub_server/main.py
"""Standalone unsubscribe server. Serves every unsub.<domain> host; the
signed token (not the Host header) identifies tenant + recipient.

Routes mirror app/api/unsub.py in cadence -- keep behavior in lockstep."""

from __future__ import annotations

import os
from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from unsub_server import db
from unsub_server.tokens import UnsubTokenError, verify

app = FastAPI(title="cadence-unsub", docs_url=None, redoc_url=None, openapi_url=None)

_PAGE_STYLE = (
    "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
    "Arial,sans-serif;background:#f7f7f8;color:#1a1a1a;margin:0;padding:40px 20px;}"
    ".card{max-width:480px;margin:40px auto;background:#fff;padding:32px;border-radius:8px;"
    "box-shadow:0 1px 3px rgba(0,0,0,0.08);}"
    "h1{font-size:20px;margin:0 0 16px;}"
    "p{line-height:1.5;margin:0 0 20px;color:#444;}"
    "button{background:#1a1a1a;color:#fff;border:0;padding:12px 24px;border-radius:6px;"
    "font-size:15px;cursor:pointer;}"
    "button:hover{background:#333;}"
    ".muted{color:#777;font-size:13px;}"
)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>"
        f"<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{escape(title)}</title>"
        f"<style>{_PAGE_STYLE}</style></head>"
        f"<body><div class=\"card\">{body}</div></body></html>"
    )


def _signing_key() -> str:
    return os.environ["CADENCE_UNSUB_SIGNING_KEY"]


@app.get("/health")
async def health() -> dict:
    await db.ping()
    return {"status": "ok"}


@app.get("/u/{token}", response_class=HTMLResponse)
async def unsub_confirm(token: str) -> HTMLResponse:
    try:
        payload = verify(token, signing_key=_signing_key())
    except UnsubTokenError:
        return HTMLResponse(
            _page("Link expired", "<h1>Link expired</h1><p>This unsubscribe link has expired or is invalid.</p>"),
            status_code=410,
        )

    recipient = await db.fetch_recipient(payload.tenant, payload.recipient_id)
    if recipient is None:
        return HTMLResponse(
            _page("Link expired", "<h1>Link expired</h1><p>This unsubscribe link is no longer valid.</p>"),
            status_code=410,
        )

    email = escape(recipient["email"])
    tenant = escape(payload.tenant)
    action = f"/u/{escape(token)}"
    body = (
        f"<h1>Unsubscribe</h1>"
        f"<p>You're about to unsubscribe <strong>{email}</strong> from {tenant} emails. "
        f"Click below to confirm.</p>"
        f"<form method=\"POST\" action=\"{action}\">"
        f"<button type=\"submit\">Unsubscribe</button></form>"
    )
    return HTMLResponse(_page("Unsubscribe", body))


@app.post("/u/{token}")
async def unsub_one_click(token: str) -> Response:
    try:
        payload = verify(token, signing_key=_signing_key())
    except UnsubTokenError:
        return Response(status_code=410)

    recipient = await db.fetch_recipient(payload.tenant, payload.recipient_id)
    if recipient is None:
        return Response(status_code=404)

    await db.record_unsub(payload.tenant, payload.recipient_id, recipient["email"])

    tenant = escape(payload.tenant)
    body = (
        f"<h1>Unsubscribed</h1>"
        f"<p>You've been unsubscribed from {tenant}.</p>"
        f"<p class=\"muted\">It may take a short time for this to propagate to in-flight sends.</p>"
    )
    return HTMLResponse(_page("Unsubscribed", body))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_unsub_server_routes.py -q`
Expected: `8 passed`

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass (315+), no new failures.

- [ ] **Step 6: Commit**

```bash
git add unsub_server/main.py tests/test_unsub_server_routes.py
git commit -m "feat: unsub_server FastAPI routes (confirm page, one-click POST, health)"
```

---

### Task 4: Deployable packaging (pyproject + Dockerfile)

**Files:**
- Create: `unsub_server/pyproject.toml`
- Create: `unsub_server/Dockerfile`
- Create: `unsub_server/README.md`

**Interfaces:**
- Consumes: `unsub_server.main:app` (Task 3).
- Produces: a buildable container; nothing downstream.

Note: this pyproject exists for *deployment* — the dev/test environment
remains cadence's root env (which already has every dependency listed).
Do not wire this pyproject into the root workspace.

- [ ] **Step 1: Write pyproject**

```toml
# unsub_server/pyproject.toml
[project]
name = "cadence-unsub-server"
version = "0.1.0"
description = "Standalone unsubscribe server for cadence sending domains"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "itsdangerous>=2.2",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.29",
    "uuid-utils>=0.9",
]
```

- [ ] **Step 2: Write Dockerfile**

```dockerfile
# unsub_server/Dockerfile
# Build from the REPO ROOT so the unsub_server package dir is in context:
#   docker build -f unsub_server/Dockerfile -t cadence-unsub .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /srv
COPY unsub_server/pyproject.toml unsub_server/pyproject.toml
RUN uv venv && uv pip install -r unsub_server/pyproject.toml
COPY unsub_server unsub_server

EXPOSE 8080
# UNSUB_DATABASE_URL and CADENCE_UNSUB_SIGNING_KEY are provided at runtime.
CMD ["uv", "run", "--no-project", "uvicorn", "unsub_server.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

- [ ] **Step 3: Write README**

```markdown
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
```

- [ ] **Step 4: Sanity check (no container build needed)**

Run: `uv run python -c "import tomllib; tomllib.load(open('unsub_server/pyproject.toml','rb')); print('toml ok')"`
Expected: `toml ok`

- [ ] **Step 5: Commit**

```bash
git add unsub_server/pyproject.toml unsub_server/Dockerfile unsub_server/README.md
git commit -m "feat: unsub_server packaging (pyproject, Dockerfile, README)"
```

---

### Task 5: Least-privilege Postgres role (SQL file only — execution is done by the main session, NOT a subagent)

**Files:**
- Create: `unsub_server/sql/create_role.sql`

**Interfaces:**
- Consumes: table names from Task 2's SQL.
- Produces: the grants script the operator runs; the resulting role backs `UNSUB_DATABASE_URL`.

- [ ] **Step 1: Write the SQL**

```sql
-- unsub_server/sql/create_role.sql
-- Least-privilege role for the standalone unsub server.
-- Run as a superuser/owner: psql -v pw='<generated password>' -f create_role.sql
CREATE ROLE unsub_server LOGIN PASSWORD :'pw';

GRANT CONNECT ON DATABASE cadence TO unsub_server;
GRANT USAGE ON SCHEMA public TO unsub_server;

GRANT SELECT (id, tenant, email, status), UPDATE (status, updated_at)
  ON recipients TO unsub_server;
GRANT SELECT, INSERT ON suppressions TO unsub_server;
GRANT SELECT, INSERT ON unsub_outbox TO unsub_server;
```

- [ ] **Step 2: Commit**

```bash
git add unsub_server/sql/create_role.sql
git commit -m "feat: unsub_server least-privilege DB role script"
```

- [ ] **Step 3 (main session only, after subagents finish):** generate a random password (`python -c "import secrets; ..."`, never printed to chat/logs), run the script against the shared DB using the existing admin credentials from `.env`, append `UNSUB_DATABASE_URL` to the gitignored `.env`, and verify by connecting as `unsub_server` and running the Task 2 `ping()` plus a denied `SELECT * FROM sends` (expect permission error). Adjust the database name in the GRANT CONNECT line to the actual database in `CADENCE_DATABASE_URL` first.

---

## Self-Review Notes

- Spec coverage: endpoints (T3), token contract + cross-checks (T1), raw-SQL layer + schema cross-checks (T2), packaging/Docker (T4), DB role (T5), "no cadence-side changes" — nothing to do by design. Out-of-scope items (TLS, per-domain keys, static split) remain out.
- Type consistency: `verify(token, signing_key, max_age)` used identically in T1/T3; `fetch_recipient -> dict | None` keys (`id`, `email`, `status`) match T3's usage; `record_unsub(tenant, recipient_id, email)` matches.
- The GET-unknown-recipient case returns 410 and POST returns 404 — this asymmetry is cadence's existing behavior, mirrored deliberately.
```
