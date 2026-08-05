# OTTO Record Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a per-tenant bulk record loader (CLI first, HTTP endpoint second) that turns OTTO lead files into `recipients` + `consent_proof` + `contact_profile` rows, quarantining anything without TCPA consent.

**Architecture:** Pure mapping/normalization layer (`app/domain/`, no SQLAlchemy or FastAPI imports) turns a raw CSV/JSONL row into a `MappedRow`. A service layer (`app/services/record_ingest.py`) does all DB writes. The CLI (`app/cli/load_records.py`) and the later endpoint are both thin shells over that service. Two supporting fixes land first: the segment compiler learns `attributes.*` so vehicle data is segmentable, and its provider code maps are corrected to match the DB seed.

**Tech Stack:** Python 3.12, async SQLAlchemy 2.0 + asyncpg, Alembic, Pydantic v2, structlog, PyYAML, pytest + pytest-asyncio, uv.

Spec: `docs/superpowers/specs/2026-08-05-otto-record-ingestion-design.md`

## Global Constraints

- Python tooling is **uv only**. `uv add` for dependencies, `uv run` to execute. Never pip/poetry.
- `app/domain/` modules must not import FastAPI or SQLAlchemy (CLAUDE.md working principle 4).
- Type hints on every function signature.
- Primary keys are UUID v7 via `app.models.base.uuid7`.
- Postgres only. No Redis/Kafka/SQS/ClickHouse.
- Tests run against the docker test DB: `docker compose -f docker-compose.test.yml up -d --wait`, then `uv run pytest`. The suite self-migrates to head.
- **Any row created by a test must use an email ending in `@cadencetests.io`** — `tests/conftest.py`'s autouse `_clean_test_rows` fixture keys off that suffix.
- `contact_profile` has a `GENERATED ALWAYS AS` column and no ORM model. Write it with `sqlalchemy.text()`, matching `app/workers/profile_refresh.py` and `app/api/origin_platform.py`.
- Commit messages: imperative mood, conventional prefix, **no** Co-Authored-By or "Generated with" line.
- Do not push to main. Commit locally only.
- Current Alembic head: `seg7a_campaign_segment_key`.

---

### Task 1: consent_proof dedup index

Makes a full file re-run safe: re-importing the same consent event inserts no duplicate proof.

**Files:**
- Create: `alembic/versions/2026_08_05_1000-seg8a_consent_proof_dedup.py`
- Test: `tests/workers/test_consent_proof_dedup.py`

**Interfaces:**
- Consumes: nothing.
- Produces: unique index `uq_consent_proof_dedup` on `consent_proof (tenant, contact_id, disclosure_text_hash, captured_at)`. Task 7 relies on it for `ON CONFLICT ... DO NOTHING`.

- [ ] **Step 1: Write the failing test**

```python
# tests/workers/test_consent_proof_dedup.py
"""The dedup index is what makes a full re-import safe (spec §7)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.core.db import async_session_factory
from app.models.base import uuid7

_TENANT = "otto"


@pytest.mark.asyncio
async def test_duplicate_consent_proof_is_deduped():
    contact_id = uuid7()
    captured_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with async_session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO recipients (id, tenant, email, vertical, first_seen_at,"
                " last_seen_at, consent_source, consent_at)"
                " VALUES (:id, :t, :e, 'auto', now(), now(), 'otto_tcpa', now())"
            ),
            {"id": str(contact_id), "t": _TENANT, "e": f"dedup-{uuid.uuid4()}@cadencetests.io"},
        )
        insert = text(
            "INSERT INTO consent_proof (consent_proof_id, tenant, contact_id, captured_at,"
            " disclosure_text_hash, channels)"
            " VALUES (:id, :t, :c, :cap, :h, ARRAY[1]::smallint[])"
            " ON CONFLICT (tenant, contact_id, disclosure_text_hash, captured_at) DO NOTHING"
        )
        params = {"t": _TENANT, "c": str(contact_id), "cap": captured_at, "h": "abc123"}
        await s.execute(insert, {**params, "id": str(uuid7())})
        await s.execute(insert, {**params, "id": str(uuid7())})
        await s.commit()

        count = (
            await s.execute(
                text(
                    "SELECT count(*) FROM consent_proof"
                    " WHERE tenant = :t AND contact_id = :c"
                ),
                {"t": _TENANT, "c": str(contact_id)},
            )
        ).scalar_one()
        await s.execute(
            text("DELETE FROM consent_proof WHERE contact_id = :c"), {"c": str(contact_id)}
        )
        await s.execute(text("DELETE FROM recipients WHERE id = :c"), {"c": str(contact_id)})
        await s.commit()

    assert count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/workers/test_consent_proof_dedup.py -v`
Expected: FAIL — `ProgrammingError: there is no unique or exclusion constraint matching the ON CONFLICT specification`.

- [ ] **Step 3: Write the migration**

```python
# alembic/versions/2026_08_05_1000-seg8a_consent_proof_dedup.py
"""seg8a: unique index for consent_proof re-import dedup.

Revision ID: seg8a_consent_proof_dedup
Revises: seg7a_campaign_segment_key
Create Date: 2026-08-05 10:00:00

----
The bulk record loader has no resume ledger; recovery from a failed run is a
full re-run of the file. consent_proof is append-only evidence, so a re-run
would otherwise duplicate every proof. Genuinely distinct consent events differ
in captured_at, so only literal re-imports of the same event collapse.
----
"""

from __future__ import annotations

from alembic import op

revision = "seg8a_consent_proof_dedup"
down_revision = "seg7a_campaign_segment_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_consent_proof_dedup"
        " ON consent_proof (tenant, contact_id, disclosure_text_hash, captured_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_consent_proof_dedup")
```

- [ ] **Step 4: Apply and re-run the test**

Run: `uv run alembic upgrade head && uv run pytest tests/workers/test_consent_proof_dedup.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/2026_08_05_1000-seg8a_consent_proof_dedup.py tests/workers/test_consent_proof_dedup.py
git commit -m "feat: unique index for consent_proof re-import dedup"
```

---

### Task 2: correct the provider code maps

`app/services/segment_query.py:42-60` disagrees with `app/domain/isp.py:23-41` and the `domain_provider` seed. `cable` compiles to 5, which is `apple` in the DB — `segments/email/cable_dormant.yaml` currently selects the wrong contacts.

**Files:**
- Modify: `app/services/segment_query.py:41-61`
- Modify: `tests/workers/test_segment_materialize.py:38` (seed constants)
- Test: `tests/domain/test_segment_sql.py` (append)

**Interfaces:**
- Consumes: `app.domain.isp.MAILBOX_PROVIDER_CODES`, `app.domain.isp.REPUTATION_GROUP_CODES`.
- Produces: `segment_query.MAILBOX_PROVIDER_CODES` / `REPUTATION_GROUP_CODES` agreeing with `isp.py` for every shared key. Task 7 writes codes from `isp.py`; this is what makes them readable.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/domain/test_segment_sql.py
from app.domain.isp import MAILBOX_PROVIDER_CODES as ISP_MP_CODES
from app.domain.isp import REPUTATION_GROUP_CODES as ISP_RG_CODES
from app.services.segment_query import MAILBOX_PROVIDER_CODES, REPUTATION_GROUP_CODES


def test_provider_code_maps_agree_with_isp():
    """The compiler must emit the codes the ingest path actually writes.

    isp.py mirrors the domain_provider seed in
    alembic/versions/2026_07_30_1500-seg1_domain_provider.py.
    """
    for name, code in ISP_MP_CODES.items():
        assert MAILBOX_PROVIDER_CODES[name] == code, name
    for name, code in ISP_RG_CODES.items():
        assert REPUTATION_GROUP_CODES[name] == code, name


def test_cable_compiles_to_the_seeded_cable_code():
    sql, params = compile_segment(
        {"all": [{"field": "mailbox_provider", "eq": "cable"}]},
        _DEFAULT_CONFIG,
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert 4 in params.values()
    assert 5 not in params.values()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/domain/test_segment_sql.py -k "provider_code_maps or cable_compiles" -v`
Expected: FAIL — `assert 5 == 4` for `cable`.

- [ ] **Step 3: Replace the two maps**

```python
# app/services/segment_query.py — replace lines 41-61
# Provider codes are owned by app/domain/isp.py, which mirrors the
# domain_provider seed in seg1_domain_provider. Importing rather than
# re-declaring keeps the compiler and the ingest path from drifting.
from app.domain.isp import MAILBOX_PROVIDER_CODES as _ISP_MAILBOX_PROVIDER_CODES
from app.domain.isp import REPUTATION_GROUP_CODES as _ISP_REPUTATION_GROUP_CODES

# "outlook" is kept as a YAML-facing alias for microsoft; older segment files
# use it. There is no "unknown" provider — unclassified domains are "other" (0).
MAILBOX_PROVIDER_CODES: dict[str, int] = {
    **_ISP_MAILBOX_PROVIDER_CODES,
    "outlook": _ISP_MAILBOX_PROVIDER_CODES["microsoft"],
}

REPUTATION_GROUP_CODES: dict[str, int] = {
    **_ISP_REPUTATION_GROUP_CODES,
    "outlook": _ISP_REPUTATION_GROUP_CODES["microsoft"],
}
```

Put the two `from app.domain.isp import ...` lines with the other imports at the top of the file, not inline.

- [ ] **Step 4: Fix the materialize test seed constants**

`tests/workers/test_segment_materialize.py:38` seeds rows using the old codes. Update its provider constants to `gmail=1, microsoft=2, yahoo=3, cable=4, apple=5, corporate=6, other=0` and update the comment to say the codes come from `app.domain.isp`.

- [ ] **Step 5: Run the full segment suite**

Run: `uv run pytest tests/domain/test_segment_sql.py tests/domain/test_segment_dsl.py tests/workers/test_segment_materialize.py -v`
Expected: PASS. If another test asserts on the old codes, fix that test — `isp.py` and the seed migration are ground truth.

- [ ] **Step 6: Commit**

```bash
git add app/services/segment_query.py tests/domain/test_segment_sql.py tests/workers/test_segment_materialize.py
git commit -m "fix: align segment compiler provider codes with domain_provider seed"
```

---

### Task 3: `attributes.*` fields and the `contains` operator

Without this, none of OTTO's vehicle data is reachable from `segments/email/*.yaml`.

**Files:**
- Modify: `app/domain/segment.py` (operator sets; `_eval_clause`)
- Modify: `app/services/segment_query.py` (`_col`, `_compile_clause`)
- Test: `tests/domain/test_segment_dsl.py` (append), `tests/domain/test_segment_sql.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: segment DSL accepts `{"field": "attributes.<key>", ...}` and a `contains` operator. Task 6 emits `vehicle_count`, `vehicle_makes`, `newest_vehicle_year` into `recipients.attributes` against this contract.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/domain/test_segment_dsl.py
from app.domain.segment import matches, normalize


def test_contains_matches_list_valued_attribute():
    seg = {"all": [{"field": "attributes.vehicle_makes", "op": "contains", "value": "toyota"}]}
    assert matches(seg, {"vehicle_makes": ["toyota", "honda"]}, "auto") is True
    assert matches(seg, {"vehicle_makes": ["ford"]}, "auto") is False


def test_contains_on_scalar_attribute_is_false_not_an_error():
    seg = {"all": [{"field": "attributes.vehicle_makes", "op": "contains", "value": "toyota"}]}
    assert matches(seg, {"vehicle_makes": "toyota"}, "auto") is False


def test_contains_on_missing_attribute_is_false():
    seg = {"all": [{"field": "attributes.vehicle_makes", "op": "contains", "value": "toyota"}]}
    assert matches(seg, {}, "auto") is False


def test_normalize_accepts_contains():
    assert normalize(
        {"all": [{"field": "attributes.vehicle_makes", "op": "contains", "value": "toyota"}]}
    ) == {"all": [{"field": "attributes.vehicle_makes", "contains": "toyota"}]}
```

```python
# append to tests/domain/test_segment_sql.py
def test_attributes_numeric_field_compiles_to_a_cast_subquery():
    sql, params = compile_segment(
        {"all": [{"field": "attributes.vehicle_count", "op": "gte", "value": 2}]},
        _DEFAULT_CONFIG,
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert "FROM recipients r" in sql
    assert "r.id = cp.contact_id" in sql
    assert "r.tenant = cp.tenant" in sql
    assert "::numeric" in sql
    assert "vehicle_count" in params.values()
    assert 2 in params.values()


def test_attributes_contains_compiles_to_jsonb_exists():
    sql, params = compile_segment(
        {"all": [{"field": "attributes.vehicle_makes", "op": "contains", "value": "toyota"}]},
        _DEFAULT_CONFIG,
        include_unsendable=True,
        acknowledges_cold=True,
    )
    assert "jsonb_exists(" in sql
    assert "?" not in sql  # the ? operator collides with driver placeholders
    assert "toyota" in params.values()


def test_attributes_key_is_validated():
    with pytest.raises(ValueError, match="invalid attributes key"):
        compile_segment(
            {"all": [{"field": "attributes.bad key'; drop table", "op": "eq", "value": 1}]},
            _DEFAULT_CONFIG,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/domain/test_segment_dsl.py tests/domain/test_segment_sql.py -k "contains or attributes" -v`
Expected: FAIL — `unknown operator 'contains'` and `unsupported segment field`.

- [ ] **Step 3: Add `contains` to the pure matcher**

```python
# app/domain/segment.py — add "contains" to the v3 operator tuple (line 57)
_V3_OPERATORS = (
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "within",
    "not_within",
    "is_null",
    "is_not_null",
    "between",
    "contains",
)
```

```python
# app/domain/segment.py — in _eval_clause, directly after the "not_in" branch
    if "contains" in clause:
        # List membership. A scalar actual never "contains" anything — the
        # scalar case is what `eq` is for.
        return isinstance(actual, (list, tuple)) and clause["contains"] in actual
```

- [ ] **Step 4: Add `attributes.*` and `contains` to the SQL compiler**

```python
# app/services/segment_query.py — near the other module constants
_ATTR_PREFIX = "attributes."
_ATTR_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")

# Operators that compare numerically and therefore need a cast off jsonb text.
_NUMERIC_OPS = frozenset({"gt", "gte", "lt", "lte", "between"})


def _attr_subquery(key: str, bag: _ParamBag, *, as_json: bool) -> str:
    """Correlated scalar subquery for a recipients.attributes key.

    Returns the raw jsonb value when ``as_json`` (needed by jsonb_exists and
    NULL checks), otherwise the text projection.  The key is bound as a
    parameter, never interpolated.
    """
    if not _ATTR_KEY_RE.match(key):
        raise ValueError(f"invalid attributes key: {key!r}")
    p = bag.add(key)
    op = "->" if as_json else "->>"
    return (
        f"(SELECT r.attributes {op} :{p} FROM recipients r"
        f" WHERE r.id = cp.contact_id AND r.tenant = cp.tenant)"
    )
```

Replace `_col(field)` usage in `_compile_clause` with a resolver that knows about attributes. Insert this immediately after `field: str = node["field"]` (line 176), replacing the `col = _col(field)` line:

```python
    is_attr = field.startswith(_ATTR_PREFIX)
    if is_attr:
        attr_key = field[len(_ATTR_PREFIX):]
        # Validate eagerly so a bad key fails even for operators that build
        # their own expression below.
        if not _ATTR_KEY_RE.match(attr_key):
            raise ValueError(f"invalid attributes key: {attr_key!r}")
        op_name = next((k for k in node if k != "field"), "")
        if op_name in _NUMERIC_OPS:
            col = f"({_attr_subquery(attr_key, bag, as_json=False)})::numeric"
        elif op_name in ("is_null", "is_not_null", "contains"):
            col = _attr_subquery(attr_key, bag, as_json=True)
        else:
            col = _attr_subquery(attr_key, bag, as_json=False)
    else:
        col = _col(field)
```

`_coerce_value` must not translate attribute values into smallint codes — guard it:

```python
def _coerce_value(field: str, value: Any) -> Any:
    """Translate string values to smallint codes for coded fields."""
    if field.startswith(_ATTR_PREFIX):
        return value
    codes = _SMALLINT_FIELDS.get(field)
    ...
```

Add the `contains` branch to `_compile_clause`, after `not_in`:

```python
    if "contains" in node:
        p = bag.add(node["contains"])
        # Function form, not the `?` operator: `?` collides with the driver's
        # placeholder syntax.
        return f"jsonb_exists({col}, :{p})"
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/domain/test_segment_dsl.py tests/domain/test_segment_sql.py -v`
Expected: PASS, including the pre-existing tests.

- [ ] **Step 6: Commit**

```bash
git add app/domain/segment.py app/services/segment_query.py tests/domain/test_segment_dsl.py tests/domain/test_segment_sql.py
git commit -m "feat: segment support for attributes.* fields and contains operator"
```

---

### Task 4: field normalizers

**Files:**
- Create: `app/domain/normalizers.py`
- Test: `tests/domain/test_normalizers.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `normalize_email(value: object) -> str | None`
  - `normalize_name(value: object) -> str | None`
  - `normalize_state(value: object) -> str | None`
  - `normalize_zip5(value: object) -> str | None`
  - `normalize_phone_e164(value: object) -> str | None`
  - `normalize_year(value: object) -> int | None`
  - `NORMALIZERS: dict[str, Callable[[object], object]]` keyed by the YAML `normalize:` name (`"name"`, `"state"`, `"zip5"`, `"phone_e164"`, `"year"`, `"email"`).

  Every normalizer returns `None` for input it cannot make sense of, and never raises. Task 6 treats `None` on a required field as an error.

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_normalizers.py
"""Pure normalizer tests — no DB, no app wiring."""

from __future__ import annotations

import pytest

from app.domain.normalizers import (
    NORMALIZERS,
    normalize_email,
    normalize_name,
    normalize_phone_e164,
    normalize_state,
    normalize_year,
    normalize_zip5,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("(512) 555-1234", "+15125551234"),
        ("512-555-1234", "+15125551234"),
        ("15125551234", "+15125551234"),
        ("+1 512 555 1234", "+15125551234"),
        ("5551234", None),          # 7 digits, no area code
        ("512-555-1234 x22", None), # extension: ambiguous, reject
        ("", None),
        (None, None),
        ("not a phone", None),
    ],
)
def test_normalize_phone_e164(raw, expected):
    assert normalize_phone_e164(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("78701", "78701"),
        ("78701-1234", "78701"),
        ("787011234", "78701"),
        ("1234", "01234"),   # Excel stripped the leading zero
        ("501", "00501"),
        ("  78701  ", "78701"),
        ("", None),
        (None, None),
        ("ABCDE", None),
    ],
)
def test_normalize_zip5(raw, expected):
    assert normalize_zip5(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("TX", "TX"),
        ("tx", "TX"),
        ("Texas", "TX"),
        ("  new york ", "NY"),
        ("DC", "DC"),
        ("ZZ", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_state(raw, expected):
    assert normalize_state(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2019", 2019),
        (2019, 2019),
        (" 2019 ", 2019),
        ("N/A", None),
        ("", None),
        (None, None),
        ("1799", None),   # out of range
        ("2200", None),
    ],
)
def test_normalize_year(raw, expected):
    assert normalize_year(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SHAWN", "Shawn"),
        ("shawn", "Shawn"),
        ("McDonald", "McDonald"),   # mixed case is left alone
        ("  shawn   estes ", "Shawn Estes"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Shawn@Example.COM ", "shawn@example.com"),
        ("shawn@example.com", "shawn@example.com"),
        ("shawn@localhost", None),
        ("shawn", None),
        ("a@b@c.com", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


def test_normalizers_registry_covers_every_yaml_name():
    assert set(NORMALIZERS) == {"email", "name", "state", "zip5", "phone_e164", "year"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/domain/test_normalizers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.domain.normalizers'`.

- [ ] **Step 3: Write the implementation**

```python
# app/domain/normalizers.py
"""Pure field normalizers for tenant record ingestion.

Every normalizer accepts whatever a CSV/JSONL cell produced (str, int, None)
and returns a canonical value or None. None means "this cell held nothing
usable" — callers decide whether that is fatal for the row.

No FastAPI, no SQLAlchemy: these are unit-testable without a DB.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_NON_DIGITS = re.compile(r"\D+")
_WHITESPACE = re.compile(r"\s+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

_MIN_YEAR = 1900
_MAX_YEAR = 2100

_STATE_ABBREVS: frozenset[str] = frozenset(
    """AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN
    MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV
    WI WY PR VI GU AS MP""".split()
)

_STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR",
}


def _text(value: object) -> str | None:
    if value is None:
        return None
    s = _WHITESPACE.sub(" ", str(value)).strip()
    return s or None


def normalize_email(value: object) -> str | None:
    s = _text(value)
    if s is None:
        return None
    s = s.lower()
    return s if _EMAIL_RE.match(s) else None


def normalize_name(value: object) -> str | None:
    """Collapse whitespace; case-correct only all-upper or all-lower input.

    "McDonald" and "van der Berg" are left alone — title-casing them destroys
    information that the source already got right.
    """
    s = _text(value)
    if s is None:
        return None
    if s.isupper() or s.islower():
        return s.title()
    return s


def normalize_state(value: object) -> str | None:
    s = _text(value)
    if s is None:
        return None
    upper = s.upper()
    if upper in _STATE_ABBREVS:
        return upper
    return _STATE_NAMES.get(s.lower())


def normalize_zip5(value: object) -> str | None:
    """Return a 5-digit ZIP.

    Handles ZIP+4 ("78701-1234", "787011234") and spreadsheet-stripped leading
    zeros ("1234" -> "01234").
    """
    s = _text(value)
    if s is None:
        return None
    digits = _NON_DIGITS.sub("", s)
    if len(digits) == 9:
        digits = digits[:5]
    if 3 <= len(digits) <= 5:
        return digits.zfill(5)
    return None


def normalize_phone_e164(value: object) -> str | None:
    """US 10-digit numbers only, rendered +1XXXXXXXXXX.

    An 11-digit number leading with 1 is accepted. Anything else — 7-digit
    local numbers, extensions, international — is rejected rather than guessed.
    """
    s = _text(value)
    if s is None:
        return None
    digits = _NON_DIGITS.sub("", s)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        return None
    return f"+1{digits}"


def normalize_year(value: object) -> int | None:
    s = _text(value)
    if s is None:
        return None
    try:
        year = int(float(s))
    except (TypeError, ValueError):
        return None
    return year if _MIN_YEAR <= year <= _MAX_YEAR else None


NORMALIZERS: dict[str, Callable[[object], object]] = {
    "email": normalize_email,
    "name": normalize_name,
    "state": normalize_state,
    "zip5": normalize_zip5,
    "phone_e164": normalize_phone_e164,
    "year": normalize_year,
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/domain/test_normalizers.py -v`
Expected: PASS (36 parametrized cases).

- [ ] **Step 5: Commit**

```bash
git add app/domain/normalizers.py tests/domain/test_normalizers.py
git commit -m "feat: pure field normalizers for record ingestion"
```

---

### Task 5: tenant mapping loader + otto.yaml

**Files:**
- Create: `config/tenants/otto.yaml`
- Create: `app/domain/tenant_spec.py` (the frozen dataclasses)
- Create: `app/services/tenant_mapping.py` (file IO + validation)
- Test: `tests/services/test_tenant_mapping.py`
- Modify: `pyproject.toml` (declare `pyyaml`)

**Why the split:** `app/domain/record_mapping.py` (Task 6) needs the mapping types, and
`app/services/tenant_mapping.py` imports `app.domain.normalizers`. Putting the types in
`app/services/` would make the domain layer import the service layer and create an import
cycle. The dataclasses live in `app/domain/tenant_spec.py`; only the YAML reading lives in
`app/services/`.

**Interfaces:**
- Consumes: `app.domain.normalizers.NORMALIZERS` (validates that every `normalize:` name exists).
- Produces — in `app/domain/tenant_spec.py` (all frozen dataclasses):
  - `FieldSpec(source: str, normalize: str | None, required: bool)`
  - `RepeatingSpec(name: str, column_template: str, json_key: str, fields: tuple[str, ...], max: int)`
  - `DerivedSpec(name: str, func: str, source: str, field: str | None)` — `func` is `"count" | "list" | "max"`
  - `ConsentSpec(channels: tuple[int, ...], required: bool, source_label: str, text: FieldSpec, captured_at: FieldSpec, optional: dict[str, FieldSpec])` — `optional` keys are `source_url`, `form_id`, `ip`, `user_agent`
  - `TenantMapping(tenant, email_source, external_id_source, vertical_source, fields: dict[str, FieldSpec], repeating: tuple[RepeatingSpec, ...], derived: tuple[DerivedSpec, ...], consent: ConsentSpec)`

  and in `app/services/tenant_mapping.py`:
  - `load_tenant_mapping(tenant: str, mappings_dir: Path | None = None) -> TenantMapping` — raises `ValueError` on an invalid file, caches by `(tenant, dir)`.

  Task 6 consumes `TenantMapping`.

**Dependency note:** `pyyaml` is already imported by `app/services/segment_loader.py:17` but is only a transitive entry in `uv.lock` — it is not declared in `pyproject.toml`. This task declares it. That is making an existing undeclared import explicit, not adding a new package.

- [ ] **Step 1: Declare pyyaml**

Run: `uv add pyyaml`
Expected: `pyproject.toml` gains `"pyyaml>=6"`, `uv.lock` updates.

- [ ] **Step 2: Write the failing test**

```python
# tests/services/test_tenant_mapping.py
"""Tenant mapping YAML loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.tenant_mapping import load_tenant_mapping

_REPO_ROOT = Path(__file__).parents[2]


def test_loads_otto_mapping_from_repo_config():
    m = load_tenant_mapping("otto")
    assert m.tenant == "otto"
    assert m.email_source == "email"
    assert m.external_id_source == "user_id"
    assert m.fields["state"].normalize == "state"
    assert m.fields["phone"].required is False
    assert m.consent.required is True
    assert m.consent.channels == (1,)
    assert m.consent.source_label == "otto_tcpa"
    assert m.consent.text.source == "tcpa_text"
    assert m.consent.captured_at.source == "tcpa_consent_at"
    assert "consent_url" == m.consent.optional["source_url"].source


def test_otto_repeating_and_derived():
    m = load_tenant_mapping("otto")
    (vehicles,) = m.repeating
    assert vehicles.name == "vehicles"
    assert vehicles.column_template == "vehicle_{n}_{field}"
    assert vehicles.fields == ("year", "make", "model", "vin")
    assert vehicles.max == 5
    names = {d.name: (d.func, d.source, d.field) for d in m.derived}
    assert names["vehicle_count"] == ("count", "vehicles", None)
    assert names["vehicle_makes"] == ("list", "vehicles", "make")
    assert names["newest_vehicle_year"] == ("max", "vehicles", "year")


def test_unknown_tenant_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="no mapping file"):
        load_tenant_mapping("nosuchtenant", mappings_dir=tmp_path)


def test_unknown_normalizer_raises(tmp_path: Path):
    (tmp_path / "acme.yaml").write_text(
        "tenant: acme\n"
        "identity:\n  email: email\n"
        "fields:\n  city: {from: city, normalize: nonesuch}\n"
        "consent:\n"
        "  channels: [1]\n"
        "  source_label: acme\n"
        "  text: {from: t}\n"
        "  captured_at: {from: c}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown normalizer 'nonesuch'"):
        load_tenant_mapping("acme", mappings_dir=tmp_path)


def test_missing_consent_block_raises(tmp_path: Path):
    (tmp_path / "acme.yaml").write_text(
        "tenant: acme\nidentity:\n  email: email\nfields: {}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="consent"):
        load_tenant_mapping("acme", mappings_dir=tmp_path)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/services/test_tenant_mapping.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.tenant_mapping'`.

- [ ] **Step 4: Write `config/tenants/otto.yaml`**

```yaml
# OTTO auto-insurance lead records.
# Segment authors reach these via attributes.<key> (see SPEC-segmentation §7).
tenant: otto
identity:
  email: email
  external_id: user_id
fields:
  first_name: {from: first_name, normalize: name}
  last_name:  {from: last_name,  normalize: name}
  address1:   {from: address}
  city:       {from: city}
  state:      {from: state, normalize: state}
  zip:        {from: zip,   normalize: zip5}
  phone:      {from: phone, normalize: phone_e164, required: false}
repeating:
  vehicles:
    columns: "vehicle_{n}_{field}"
    json_key: vehicles
    fields: [year, make, model, vin]
    max: 5
derived:
  vehicle_count:       count(vehicles)
  vehicle_makes:       list(vehicles.make)
  newest_vehicle_year: max(vehicles.year)
consent:
  channels: [1]
  required: true
  source_label: otto_tcpa
  text:        {from: tcpa_text}
  captured_at: {from: tcpa_consent_at}
  source_url:  {from: consent_url, required: false}
  form_id:     {from: lead_id,     required: false}
  ip:          {from: consent_ip,  required: false}
  user_agent:  {from: consent_ua,  required: false}
```

- [ ] **Step 5: Write the mapping types**

```python
# app/domain/tenant_spec.py
"""Frozen types describing a tenant's record shape.

Lives in the domain layer so app/domain/record_mapping.py can consume it
without importing the service layer. No FastAPI, no SQLAlchemy, no YAML —
parsing is app/services/tenant_mapping.py's job.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    source: str
    normalize: str | None = None
    required: bool = True


@dataclass(frozen=True)
class RepeatingSpec:
    name: str
    column_template: str
    json_key: str
    fields: tuple[str, ...]
    max: int


@dataclass(frozen=True)
class DerivedSpec:
    name: str
    func: str          # "count" | "list" | "max"
    source: str        # repeating-group name
    field: str | None  # None for count()


@dataclass(frozen=True)
class ConsentSpec:
    channels: tuple[int, ...]
    required: bool
    source_label: str
    text: FieldSpec
    captured_at: FieldSpec
    optional: dict[str, FieldSpec]


@dataclass(frozen=True)
class TenantMapping:
    tenant: str
    email_source: str
    external_id_source: str | None
    vertical_source: str | None
    fields: dict[str, FieldSpec]
    repeating: tuple[RepeatingSpec, ...]
    derived: tuple[DerivedSpec, ...]
    consent: ConsentSpec
```

- [ ] **Step 6: Write the loader**

```python
# app/services/tenant_mapping.py
"""Per-tenant record mapping loaded from config/tenants/<tenant>.yaml.

Mirrors app/services/segment_loader.py: YAML on disk, validated into frozen
dataclasses, cached per process. A new tenant is a new YAML file, not code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from app.domain.normalizers import NORMALIZERS
from app.domain.tenant_spec import (
    ConsentSpec,
    DerivedSpec,
    FieldSpec,
    RepeatingSpec,
    TenantMapping,
)

# app/services/tenant_mapping.py -> app/services -> app -> repo root
_REPO_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_MAPPINGS_DIR = _REPO_ROOT / "config" / "tenants"

_DERIVED_RE = re.compile(r"^(count|list|max)\(([a-z0-9_]+)(?:\.([a-z0-9_]+))?\)$")

_CONSENT_OPTIONAL_KEYS = ("source_url", "form_id", "ip", "user_agent")

_cache: dict[tuple[str, str], TenantMapping] = {}


def _field_spec(raw: Any, where: str) -> FieldSpec:
    if isinstance(raw, str):
        return FieldSpec(source=raw)
    if not isinstance(raw, dict) or "from" not in raw:
        raise ValueError(f"{where}: expected a string or a mapping with a 'from' key")
    normalize = raw.get("normalize")
    if normalize is not None and normalize not in NORMALIZERS:
        raise ValueError(
            f"{where}: unknown normalizer {normalize!r}; "
            f"known: {sorted(NORMALIZERS)}"
        )
    return FieldSpec(
        source=raw["from"],
        normalize=normalize,
        required=bool(raw.get("required", True)),
    )


def _parse_consent(raw: Any) -> ConsentSpec:
    if not isinstance(raw, dict):
        raise ValueError("consent: block is required and must be a mapping")
    for key in ("text", "captured_at", "source_label"):
        if key not in raw:
            raise ValueError(f"consent: missing required key {key!r}")
    channels = tuple(int(c) for c in raw.get("channels", [1]))
    if not channels:
        raise ValueError("consent: channels must not be empty")
    optional = {
        key: _field_spec(raw[key], f"consent.{key}")
        for key in _CONSENT_OPTIONAL_KEYS
        if key in raw
    }
    return ConsentSpec(
        channels=channels,
        required=bool(raw.get("required", True)),
        source_label=str(raw["source_label"]),
        text=_field_spec(raw["text"], "consent.text"),
        captured_at=_field_spec(raw["captured_at"], "consent.captured_at"),
        optional=optional,
    )


def _parse_repeating(raw: Any) -> tuple[RepeatingSpec, ...]:
    if not raw:
        return ()
    specs: list[RepeatingSpec] = []
    for name, body in raw.items():
        if "columns" not in body or "fields" not in body:
            raise ValueError(f"repeating.{name}: 'columns' and 'fields' are required")
        specs.append(
            RepeatingSpec(
                name=name,
                column_template=body["columns"],
                json_key=body.get("json_key", name),
                fields=tuple(body["fields"]),
                max=int(body.get("max", 5)),
            )
        )
    return tuple(specs)


def _parse_derived(raw: Any, repeating: tuple[RepeatingSpec, ...]) -> tuple[DerivedSpec, ...]:
    if not raw:
        return ()
    known = {r.name for r in repeating}
    specs: list[DerivedSpec] = []
    for name, expr in raw.items():
        m = _DERIVED_RE.match(str(expr).strip())
        if m is None:
            raise ValueError(
                f"derived.{name}: expected count(<group>), list(<group>.<field>) "
                f"or max(<group>.<field>), got {expr!r}"
            )
        func, source, field = m.group(1), m.group(2), m.group(3)
        if source not in known:
            raise ValueError(f"derived.{name}: unknown repeating group {source!r}")
        if func in ("list", "max") and field is None:
            raise ValueError(f"derived.{name}: {func}() requires a <group>.<field>")
        specs.append(DerivedSpec(name=name, func=func, source=source, field=field))
    return tuple(specs)


def load_tenant_mapping(tenant: str, mappings_dir: Path | None = None) -> TenantMapping:
    """Load and validate config/tenants/<tenant>.yaml.

    Raises:
        ValueError: if the file is missing or fails validation.
    """
    directory = mappings_dir or _DEFAULT_MAPPINGS_DIR
    cache_key = (tenant, str(directory))
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    path = directory / f"{tenant}.yaml"
    if not path.is_file():
        raise ValueError(f"no mapping file for tenant {tenant!r} at {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    identity = raw.get("identity") or {}
    if "email" not in identity:
        raise ValueError(f"{path}: identity.email is required")

    repeating = _parse_repeating(raw.get("repeating"))
    mapping = TenantMapping(
        tenant=raw.get("tenant", tenant),
        email_source=identity["email"],
        external_id_source=identity.get("external_id"),
        vertical_source=identity.get("vertical"),
        fields={
            name: _field_spec(body, f"fields.{name}")
            for name, body in (raw.get("fields") or {}).items()
        },
        repeating=repeating,
        derived=_parse_derived(raw.get("derived"), repeating),
        consent=_parse_consent(raw.get("consent")),
    )
    _cache[cache_key] = mapping
    return mapping
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/services/test_tenant_mapping.py -v`
Expected: PASS. Create `tests/services/__init__.py` only if the rest of `tests/` uses package dirs — it does not, so no `__init__.py`.

- [ ] **Step 8: Commit**

```bash
git add config/tenants/otto.yaml app/domain/tenant_spec.py app/services/tenant_mapping.py tests/services/test_tenant_mapping.py pyproject.toml uv.lock
git commit -m "feat: per-tenant record mapping config and loader"
```

---

### Task 6: row mapping

**Files:**
- Create: `app/domain/record_mapping.py`
- Test: `tests/domain/test_record_mapping.py`

**Interfaces:**
- Consumes: `app.domain.tenant_spec.TenantMapping` / `RepeatingSpec` / `DerivedSpec` (Task 5), `NORMALIZERS` (Task 4). Import the types from `app.domain.tenant_spec`, never from `app.services.tenant_mapping` — that direction is an import cycle.
- Produces:
  - `ConsentRecord(text: str, captured_at: datetime, channels: tuple[int, ...], source_url: str | None, form_id: str | None, ip: str | None, user_agent: str | None)`
  - `NormalizedRecord(email: str, external_id: str | None, vertical: str | None, attributes: dict, consent: ConsentRecord, consent_source: str)`
  - `MappedRow(outcome: str, errors: tuple[str, ...], record: NormalizedRecord | None)` where `outcome` is `"ok" | "quarantine" | "reject"`
  - `map_row(row: Mapping[str, Any], mapping: TenantMapping) -> MappedRow`
  - `disclosure_hash(text: str) -> str` — SHA-256 hex of whitespace-normalized text

  Task 7 consumes `MappedRow`.

Outcome rules: no usable email or an unparseable row is `"reject"`. A usable email with missing/invalid consent text or `captured_at` is `"quarantine"`. Everything else is `"ok"`. A missing **required** non-consent field is `"reject"` — it means the file does not match the mapping.

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_record_mapping.py
"""Row mapping: raw dict -> MappedRow. Pure, no DB."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.domain.record_mapping import disclosure_hash, map_row
from app.services.tenant_mapping import load_tenant_mapping

_OTTO = load_tenant_mapping("otto")

_GOOD_ROW = {
    "email": "Lead@Example.com",
    "user_id": "otto-991",
    "first_name": "SHAWN",
    "last_name": "estes",
    "address": "100 Congress Ave",
    "city": "Austin",
    "state": "texas",
    "zip": "78701-1234",
    "phone": "(512) 555-1234",
    "vehicle_1_year": "2019",
    "vehicle_1_make": "Toyota",
    "vehicle_1_model": "Camry",
    "vehicle_2_year": "2021",
    "vehicle_2_make": "HONDA",
    "vehicle_2_model": "CR-V",
    "tcpa_text": "By submitting you agree to receive emails.",
    "tcpa_consent_at": "2026-01-15T10:30:00Z",
    "consent_url": "https://otto.example/quote",
    "lead_id": "form-7",
    "consent_ip": "203.0.113.9",
    "consent_ua": "Mozilla/5.0",
}


def test_good_row_maps_to_ok():
    m = map_row(_GOOD_ROW, _OTTO)
    assert m.outcome == "ok"
    assert m.errors == ()
    r = m.record
    assert r.email == "lead@example.com"
    assert r.external_id == "otto-991"
    assert r.consent_source == "otto_tcpa"
    assert r.attributes["first_name"] == "Shawn"
    assert r.attributes["last_name"] == "Estes"
    assert r.attributes["state"] == "TX"
    assert r.attributes["zip"] == "78701"
    assert r.attributes["phone"] == "+15125551234"
    assert r.attributes["user_id"] == "otto-991"


def test_vehicles_and_derived_fields():
    r = map_row(_GOOD_ROW, _OTTO).record
    assert r.attributes["vehicles"] == [
        {"year": 2019, "make": "Toyota", "model": "Camry"},
        {"year": 2021, "make": "HONDA", "model": "CR-V"},
    ]
    assert r.attributes["vehicle_count"] == 2
    assert r.attributes["vehicle_makes"] == ["toyota", "honda"]
    assert r.attributes["newest_vehicle_year"] == 2021


def test_jsonl_vehicle_array_is_accepted():
    row = {**_GOOD_ROW}
    for k in list(row):
        if k.startswith("vehicle_"):
            del row[k]
    row["vehicles"] = [{"year": "2018", "make": "Ford", "model": "F-150"}]
    r = map_row(row, _OTTO).record
    assert r.attributes["vehicles"] == [{"year": 2018, "make": "Ford", "model": "F-150"}]
    assert r.attributes["newest_vehicle_year"] == 2018


def test_no_vehicles_yields_zero_count_and_none_year():
    row = {k: v for k, v in _GOOD_ROW.items() if not k.startswith("vehicle_")}
    r = map_row(row, _OTTO).record
    assert r.attributes["vehicles"] == []
    assert r.attributes["vehicle_count"] == 0
    assert r.attributes["vehicle_makes"] == []
    assert r.attributes["newest_vehicle_year"] is None


def test_unparseable_vehicle_year_does_not_crash_max():
    row = {**_GOOD_ROW, "vehicle_1_year": "N/A", "vehicle_2_year": ""}
    r = map_row(row, _OTTO).record
    assert r.attributes["newest_vehicle_year"] is None
    assert r.attributes["vehicle_count"] == 2


def test_vehicles_beyond_max_are_dropped():
    row = {**_GOOD_ROW}
    for n in range(3, 9):
        row[f"vehicle_{n}_make"] = f"Make{n}"
    r = map_row(row, _OTTO).record
    assert r.attributes["vehicle_count"] == 5


def test_missing_optional_phone_is_explicit_none():
    row = {**_GOOD_ROW, "phone": ""}
    r = map_row(row, _OTTO).record
    assert "phone" in r.attributes
    assert r.attributes["phone"] is None


def test_bad_email_rejects():
    m = map_row({**_GOOD_ROW, "email": "not-an-email"}, _OTTO)
    assert m.outcome == "reject"
    assert any("email" in e for e in m.errors)
    assert m.record is None


def test_missing_consent_text_quarantines():
    m = map_row({**_GOOD_ROW, "tcpa_text": "   "}, _OTTO)
    assert m.outcome == "quarantine"
    assert any("tcpa_text" in e or "consent" in e for e in m.errors)


def test_missing_consent_timestamp_quarantines():
    m = map_row({**_GOOD_ROW, "tcpa_consent_at": ""}, _OTTO)
    assert m.outcome == "quarantine"


def test_unparseable_consent_timestamp_quarantines():
    m = map_row({**_GOOD_ROW, "tcpa_consent_at": "last tuesday"}, _OTTO)
    assert m.outcome == "quarantine"


def test_consent_timestamp_without_tz_is_treated_as_utc():
    m = map_row({**_GOOD_ROW, "tcpa_consent_at": "2026-01-15 10:30:00"}, _OTTO)
    assert m.record.consent.captured_at == datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc)


def test_missing_required_field_rejects():
    row = {k: v for k, v in _GOOD_ROW.items() if k != "state"}
    m = map_row(row, _OTTO)
    assert m.outcome == "reject"
    assert any("state" in e for e in m.errors)


def test_consent_optional_fields_carry_through():
    c = map_row(_GOOD_ROW, _OTTO).record.consent
    assert c.source_url == "https://otto.example/quote"
    assert c.form_id == "form-7"
    assert c.ip == "203.0.113.9"
    assert c.user_agent == "Mozilla/5.0"
    assert c.channels == (1,)


def test_disclosure_hash_is_whitespace_insensitive_and_stable():
    a = disclosure_hash("By submitting  you agree.\n")
    b = disclosure_hash("By submitting you agree.")
    assert a == b
    assert len(a) == 64
    assert a != disclosure_hash("By submitting you agree to SMS.")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/domain/test_record_mapping.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.domain.record_mapping'`.

- [ ] **Step 3: Write the implementation**

```python
# app/domain/record_mapping.py
"""Map one raw tenant record onto Cadence's contact shape.

Pure: no FastAPI, no SQLAlchemy. Takes a dict (one CSV row or one JSONL
object) plus a TenantMapping and returns a MappedRow describing whether the
row can be loaded, must be quarantined for lack of consent, or is rejected
outright.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.domain.normalizers import NORMALIZERS, normalize_email, normalize_year
from app.domain.tenant_spec import DerivedSpec, RepeatingSpec, TenantMapping

_WHITESPACE = re.compile(r"\s+")

OUTCOME_OK = "ok"
OUTCOME_QUARANTINE = "quarantine"
OUTCOME_REJECT = "reject"


@dataclass(frozen=True)
class ConsentRecord:
    text: str
    captured_at: datetime
    channels: tuple[int, ...]
    source_url: str | None = None
    form_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True)
class NormalizedRecord:
    email: str
    external_id: str | None
    vertical: str | None
    attributes: dict[str, Any]
    consent: ConsentRecord
    consent_source: str


@dataclass(frozen=True)
class MappedRow:
    outcome: str
    errors: tuple[str, ...]
    record: NormalizedRecord | None


def disclosure_hash(text: str) -> str:
    """SHA-256 of the disclosure text, whitespace-normalized.

    Whitespace normalization keeps a re-export with different line wrapping
    from looking like a different disclosure.
    """
    return hashlib.sha256(
        _WHITESPACE.sub(" ", text).strip().encode("utf-8")
    ).hexdigest()


def _raw(row: Mapping[str, Any], key: str) -> Any:
    value = row.get(key)
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _apply(spec_normalize: str | None, value: Any) -> Any:
    if value is None:
        return None
    if spec_normalize is None:
        s = str(value).strip()
        return s or None
    return NORMALIZERS[spec_normalize](value)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive input is treated as UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _collect_vehicles(row: Mapping[str, Any], spec: RepeatingSpec) -> list[dict[str, Any]]:
    """Read a repeating group from either indexed columns or a JSON array."""
    raw_items: list[Mapping[str, Any]] = []
    json_value = row.get(spec.json_key)
    if isinstance(json_value, list):
        raw_items = [item for item in json_value if isinstance(item, Mapping)]
    else:
        for n in range(1, spec.max + 1):
            item = {
                field: _raw(row, spec.column_template.format(n=n, field=field))
                for field in spec.fields
            }
            if any(v is not None for v in item.values()):
                raw_items.append(item)

    items: list[dict[str, Any]] = []
    for raw_item in raw_items[: spec.max]:
        item: dict[str, Any] = {}
        for field in spec.fields:
            value = raw_item.get(field)
            if isinstance(value, str) and not value.strip():
                value = None
            if value is None:
                continue
            item[field] = normalize_year(value) if field == "year" else str(value).strip()
        if item:
            items.append(item)
    return items


def _derive(spec: DerivedSpec, groups: dict[str, list[dict[str, Any]]]) -> Any:
    items = groups.get(spec.source, [])
    if spec.func == "count":
        return len(items)
    values = [item.get(spec.field) for item in items]
    if spec.func == "list":
        out: list[str] = []
        for value in values:
            if value is None:
                continue
            lowered = str(value).lower()
            if lowered not in out:
                out.append(lowered)
        return out
    # max
    numeric = [v for v in values if isinstance(v, int)]
    return max(numeric) if numeric else None


def map_row(row: Mapping[str, Any], mapping: TenantMapping) -> MappedRow:
    """Map one raw record. Never raises on bad data — it reports."""
    errors: list[str] = []

    email = normalize_email(_raw(row, mapping.email_source))
    if email is None:
        return MappedRow(
            outcome=OUTCOME_REJECT,
            errors=(f"invalid or missing email in column {mapping.email_source!r}",),
            record=None,
        )

    attributes: dict[str, Any] = {}
    for name, spec in mapping.fields.items():
        value = _apply(spec.normalize, _raw(row, spec.source))
        if value is None and spec.required:
            errors.append(f"missing or invalid required field {name!r} (column {spec.source!r})")
        attributes[name] = value

    external_id = None
    if mapping.external_id_source:
        external_id = _apply(None, _raw(row, mapping.external_id_source))
        attributes[mapping.external_id_source] = external_id

    groups: dict[str, list[dict[str, Any]]] = {
        spec.name: _collect_vehicles(row, spec) for spec in mapping.repeating
    }
    for name, items in groups.items():
        attributes[name] = items
    for derived in mapping.derived:
        attributes[derived.name] = _derive(derived, groups)

    vertical = None
    if mapping.vertical_source:
        vertical = _apply(None, _raw(row, mapping.vertical_source))

    if errors:
        return MappedRow(outcome=OUTCOME_REJECT, errors=tuple(errors), record=None)

    consent_spec = mapping.consent
    text = _apply(None, _raw(row, consent_spec.text.source))
    captured_at = _parse_timestamp(_raw(row, consent_spec.captured_at.source))
    consent_errors: list[str] = []
    if text is None:
        consent_errors.append(f"missing consent text (column {consent_spec.text.source!r})")
    if captured_at is None:
        consent_errors.append(
            f"missing or unparseable consent timestamp "
            f"(column {consent_spec.captured_at.source!r})"
        )
    if consent_errors:
        return MappedRow(
            outcome=OUTCOME_QUARANTINE, errors=tuple(consent_errors), record=None
        )

    optional = {
        key: _apply(spec.normalize, _raw(row, spec.source))
        for key, spec in consent_spec.optional.items()
    }

    record = NormalizedRecord(
        email=email,
        external_id=external_id,
        vertical=vertical,
        attributes=attributes,
        consent=ConsentRecord(
            text=text,
            captured_at=captured_at,
            channels=consent_spec.channels,
            source_url=optional.get("source_url"),
            form_id=optional.get("form_id"),
            ip=optional.get("ip"),
            user_agent=optional.get("user_agent"),
        ),
        consent_source=consent_spec.source_label,
    )
    return MappedRow(outcome=OUTCOME_OK, errors=(), record=record)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/domain/test_record_mapping.py -v`
Expected: PASS (16 tests).

- [ ] **Step 5: Verify the domain layer stays pure**

Run: `uv run python -c "import ast,sys;src=open('app/domain/record_mapping.py').read();mods={n.module or '' for n in ast.walk(ast.parse(src)) if isinstance(n,ast.ImportFrom)};assert not any(m.startswith(('fastapi','sqlalchemy')) for m in mods), mods;print('pure ok')"`
Expected: `pure ok`.

- [ ] **Step 6: Commit**

```bash
git add app/domain/record_mapping.py tests/domain/test_record_mapping.py
git commit -m "feat: pure row mapping for tenant record ingestion"
```

---

### Task 7: record ingest service

The only module that writes. Everything risky lives here: the suppression guard, consent monotonicity, and provider resolution.

**Files:**
- Create: `app/services/record_ingest.py`
- Test: `tests/services/test_record_ingest.py`

**Interfaces:**
- Consumes: `MappedRow` / `NormalizedRecord` / `disclosure_hash` (Task 6), `uq_consent_proof_dedup` (Task 1), `app.domain.isp.classify_domain_two_axis`, `app.workers.domain_provider_mx_worker.enqueue_mx_lookup`.
- Produces:
  - `IngestCounters` dataclass with int fields `read, loaded, updated, quarantined, rejected, enrolled` and `.as_dict() -> dict[str, int]`
  - `async def ingest_record(session: AsyncSession, *, tenant: str, vertical: str, record: NormalizedRecord, now: datetime) -> tuple[str, str | None]` — returns `("created" | "updated", unknown_domain_or_None)`
  - `async def quarantine_row(session: AsyncSession, *, tenant: str, reason: str, raw_row: dict, now: datetime) -> None`
  - `async def flush_unknown_domains(tenant: str, domains: set[str], *, concurrency: int = 50) -> None` — awaits every MX task before returning

  Task 8 (CLI) and Task 9 (endpoint) consume all of these.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_record_ingest.py
"""DB-backed tests for the record ingest service."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.db import async_session_factory
from app.domain.record_mapping import ConsentRecord, NormalizedRecord
from app.services.record_ingest import ingest_record, quarantine_row

_TENANT = "otto"
_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _record(email: str, *, captured_at: datetime, makes: list[str] | None = None):
    return NormalizedRecord(
        email=email,
        external_id="otto-1",
        vertical=None,
        attributes={
            "first_name": "Shawn",
            "state": "TX",
            "zip": "78701",
            "phone": "+15125551234",
            "vehicles": [{"year": 2019, "make": m} for m in (makes or ["Toyota"])],
            "vehicle_count": len(makes or ["Toyota"]),
            "vehicle_makes": [m.lower() for m in (makes or ["Toyota"])],
            "newest_vehicle_year": 2019,
        },
        consent=ConsentRecord(
            text="By submitting you agree to receive emails.",
            captured_at=captured_at,
            channels=(1,),
            source_url="https://otto.example/quote",
            form_id="form-7",
            ip="203.0.113.9",
            user_agent="Mozilla/5.0",
        ),
        consent_source="otto_tcpa",
    )


async def _profile(session, email: str) -> dict | None:
    return (
        await session.execute(
            text(
                "SELECT cp.* FROM contact_profile cp JOIN recipients r"
                " ON r.id = cp.contact_id AND r.tenant = cp.tenant"
                " WHERE cp.tenant = :t AND r.email = :e"
            ),
            {"t": _TENANT, "e": email},
        )
    ).mappings().one_or_none()


@pytest.mark.asyncio
async def test_happy_path_writes_all_three_rows():
    email = f"happy-{uuid.uuid4()}@cadencetests.io"
    async with async_session_factory() as s:
        status, unknown = await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW), now=_NOW,
        )
        await s.commit()
        assert status == "created"

        r = (
            await s.execute(
                text("SELECT * FROM recipients WHERE tenant = :t AND email = :e"),
                {"t": _TENANT, "e": email},
            )
        ).mappings().one()
        assert r["consent_source"] == "otto_tcpa"
        assert r["attributes"]["vehicle_count"] == 1

        proofs = (
            await s.execute(
                text("SELECT * FROM consent_proof WHERE tenant = :t AND contact_id = :c"),
                {"t": _TENANT, "c": str(r["id"])},
            )
        ).mappings().all()
        assert len(proofs) == 1
        assert proofs[0]["raw"]["disclosure_text"].startswith("By submitting")
        assert proofs[0]["channels"] == [1]

        p = await _profile(s, email)
        assert p["consent_state"] == 1
        assert p["sendable"] is True
        assert p["consent_proof_id"] == proofs[0]["consent_proof_id"]
        assert p["email_domain"] == "cadencetests.io"


@pytest.mark.asyncio
async def test_reimport_is_idempotent_and_appends_no_duplicate_proof():
    email = f"reimport-{uuid.uuid4()}@cadencetests.io"
    rec = _record(email, captured_at=_NOW)
    async with async_session_factory() as s:
        first, _ = await ingest_record(s, tenant=_TENANT, vertical="auto", record=rec, now=_NOW)
        await s.commit()
        second, _ = await ingest_record(s, tenant=_TENANT, vertical="auto", record=rec, now=_NOW)
        await s.commit()
        assert (first, second) == ("created", "updated")

        counts = (
            await s.execute(
                text(
                    "SELECT (SELECT count(*) FROM recipients WHERE tenant=:t AND email=:e) AS r,"
                    " (SELECT count(*) FROM consent_proof cp JOIN recipients r"
                    "   ON r.id = cp.contact_id WHERE r.email = :e) AS p"
                ),
                {"t": _TENANT, "e": email},
            )
        ).mappings().one()
        assert counts["r"] == 1
        assert counts["p"] == 1


@pytest.mark.asyncio
async def test_attributes_are_replaced_not_merged():
    email = f"replace-{uuid.uuid4()}@cadencetests.io"
    async with async_session_factory() as s:
        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW, makes=["Toyota", "Honda"]), now=_NOW,
        )
        await s.commit()
        thin = _record(email, captured_at=_NOW + timedelta(days=1), makes=["Ford"])
        thin.attributes["phone"] = None
        await ingest_record(s, tenant=_TENANT, vertical="auto", record=thin, now=_NOW)
        await s.commit()

        attrs = (
            await s.execute(
                text("SELECT attributes FROM recipients WHERE tenant=:t AND email=:e"),
                {"t": _TENANT, "e": email},
            )
        ).scalar_one()
        assert attrs["phone"] is None
        assert attrs["vehicle_count"] == 1
        assert attrs["vehicle_makes"] == ["ford"]


@pytest.mark.asyncio
async def test_suppressed_contact_is_not_resurrected():
    email = f"supp-{uuid.uuid4()}@cadencetests.io"
    async with async_session_factory() as s:
        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW), now=_NOW,
        )
        await s.commit()
        await s.execute(
            text(
                "INSERT INTO suppressions (id, tenant, email, reason, source)"
                " VALUES (gen_random_uuid(), :t, :e, 'unsubscribe', 'one_click')"
            ),
            {"t": _TENANT, "e": email},
        )
        await s.commit()

        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW + timedelta(days=2)), now=_NOW,
        )
        await s.commit()

        p = await _profile(s, email)
        assert p["consent_state"] == 2
        assert p["suppressed_at"] is not None
        assert p["sendable"] is False


@pytest.mark.asyncio
async def test_older_consent_does_not_regress_consent_at():
    email = f"older-{uuid.uuid4()}@cadencetests.io"
    newer = _NOW
    older = _NOW - timedelta(days=30)
    async with async_session_factory() as s:
        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=newer), now=_NOW,
        )
        await s.commit()
        await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=older), now=_NOW,
        )
        await s.commit()

        p = await _profile(s, email)
        assert p["consent_at"] == newer


@pytest.mark.asyncio
async def test_unknown_domain_is_reported_for_mx_enqueue():
    email = f"unknown-{uuid.uuid4()}@cadencetests.io"
    async with async_session_factory() as s:
        _, unknown = await ingest_record(
            s, tenant=_TENANT, vertical="auto",
            record=_record(email, captured_at=_NOW), now=_NOW,
        )
        await s.commit()
    assert unknown == "cadencetests.io"


@pytest.mark.asyncio
async def test_quarantine_row_writes_reason_and_raw():
    async with async_session_factory() as s:
        await quarantine_row(
            s, tenant=_TENANT, reason="missing consent text",
            raw_row={"email": "q@cadencetests.io", "tcpa_text": ""}, now=_NOW,
        )
        await s.commit()
        row = (
            await s.execute(
                text(
                    "SELECT reason, raw, contact_id FROM contact_quarantine"
                    " WHERE tenant = :t AND raw->>'email' = :e"
                ),
                {"t": _TENANT, "e": "q@cadencetests.io"},
            )
        ).mappings().one()
        assert row["contact_id"] is None
        assert row["reason"] == "missing consent text"
        await s.execute(
            text("DELETE FROM contact_quarantine WHERE tenant=:t AND raw->>'email' = :e"),
            {"t": _TENANT, "e": "q@cadencetests.io"},
        )
        await s.commit()
```

- [ ] **Step 2: Extend the workers conftest cleanup to cover these tests**

`tests/workers/conftest.py` cleans `contact_profile` / `consent_proof` for `@cadencetests.io` recipients. `tests/services/` needs the same, or `_clean_test_rows` will hit FK violations. Copy `tests/workers/conftest.py` to `tests/services/conftest.py` verbatim, then add `contact_quarantine` cleanup to the copy:

```python
        await s.execute(
            text("DELETE FROM contact_quarantine WHERE tenant = :t AND raw->>'email' LIKE :pat"),
            {"t": _TENANT, "pat": "%@cadencetests.io"},
        )
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/services/test_record_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.record_ingest'`.

- [ ] **Step 4: Write the implementation**

```python
# app/services/record_ingest.py
"""DB writes for tenant record ingestion.

One row becomes three writes: recipients, consent_proof, contact_profile.
Callers (the CLI and the batch endpoint) own transaction boundaries; this
module never commits.

Two invariants live here and nowhere else:

* A re-import must never resurrect a contact who unsubscribed. Nothing else
  writes contact_profile.consent_state on unsubscribe (unsub_server writes
  suppressions and recipients.status), so the guard is applied on every write.
* contact_profile.consent_at moves forward only. Replaying a stale export must
  not regress a newer consent timestamp.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.isp import (
    MAILBOX_PROVIDER_CODES,
    REPUTATION_GROUP_CODES,
    classify_domain_two_axis,
)
from app.domain.record_mapping import NormalizedRecord, disclosure_hash
from app.models.base import uuid7

log = structlog.get_logger(__name__)

_CHANNEL_EMAIL = 1
_CONSENT_OPTED_IN = 1
_CONSENT_OPTED_OUT = 2
_SUPPRESS_REASON_UNSUBSCRIBE = 1


@dataclass
class IngestCounters:
    read: int = 0
    loaded: int = 0
    updated: int = 0
    quarantined: int = 0
    rejected: int = 0
    enrolled: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


_UPSERT_RECIPIENT = text(
    """
    INSERT INTO recipients
        (id, tenant, email, external_id, vertical, first_seen_at, last_seen_at,
         consent_source, consent_at, attributes, status)
    VALUES
        (:id, :tenant, :email, :external_id, :vertical, :now, :now,
         :consent_source, :consent_at, CAST(:attributes AS jsonb), 'active')
    ON CONFLICT (tenant, email) DO UPDATE SET
        last_seen_at   = :now,
        external_id    = COALESCE(EXCLUDED.external_id, recipients.external_id),
        consent_source = EXCLUDED.consent_source,
        consent_at     = GREATEST(recipients.consent_at, EXCLUDED.consent_at),
        -- Replace, do not merge: each file row is a complete snapshot, and a
        -- shallow `||` merge would keep a stale phone the new row omits.
        attributes     = EXCLUDED.attributes,
        updated_at     = :now
    RETURNING id, (xmax = 0) AS created
    """
)

_INSERT_CONSENT_PROOF = text(
    """
    INSERT INTO consent_proof
        (consent_proof_id, tenant, contact_id, captured_at, source_url, form_id,
         ip, user_agent, disclosure_text_hash, channels, raw)
    VALUES
        (:id, :tenant, :contact_id, :captured_at, :source_url, :form_id,
         CAST(:ip AS inet), :user_agent, :hash, CAST(:channels AS smallint[]),
         CAST(:raw AS jsonb))
    ON CONFLICT (tenant, contact_id, disclosure_text_hash, captured_at) DO NOTHING
    RETURNING consent_proof_id
    """
)

_SELECT_EXISTING_PROOF = text(
    """
    SELECT consent_proof_id FROM consent_proof
     WHERE tenant = :tenant AND contact_id = :contact_id
       AND disclosure_text_hash = :hash AND captured_at = :captured_at
    """
)

_IS_SUPPRESSED = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM suppressions WHERE tenant = :tenant AND email = :email
    ) OR EXISTS (
        SELECT 1 FROM recipients
         WHERE tenant = :tenant AND email = :email AND status = 'suppressed'
    ) AS suppressed
    """
)

_LOOKUP_DOMAIN_PROVIDER = text(
    """
    SELECT mailbox_provider, reputation_group
      FROM domain_provider
     WHERE tenant = :tenant AND domain = :domain
    """
)

_UPSERT_PROFILE = text(
    """
    INSERT INTO contact_profile
        (contact_id, tenant, channel, email_domain, mailbox_provider,
         reputation_group, consent_state, consent_at, consent_proof_id,
         suppressed_at, suppress_reason, updated_at)
    VALUES
        (:contact_id, :tenant, :channel, :email_domain, :mailbox_provider,
         :reputation_group, :consent_state, :consent_at, :consent_proof_id,
         :suppressed_at, :suppress_reason, :now)
    ON CONFLICT (tenant, contact_id, channel) DO UPDATE SET
        email_domain     = EXCLUDED.email_domain,
        mailbox_provider = EXCLUDED.mailbox_provider,
        reputation_group = EXCLUDED.reputation_group,
        -- Never move an opted-out profile back to opted-in.
        consent_state    = LEAST(contact_profile.consent_state, EXCLUDED.consent_state),
        -- Never clear an existing suppression.
        suppressed_at    = COALESCE(contact_profile.suppressed_at, EXCLUDED.suppressed_at),
        suppress_reason  = COALESCE(contact_profile.suppress_reason, EXCLUDED.suppress_reason),
        consent_at       = GREATEST(contact_profile.consent_at, EXCLUDED.consent_at),
        consent_proof_id = CASE
            WHEN contact_profile.consent_at IS NULL
              OR EXCLUDED.consent_at > contact_profile.consent_at
            THEN EXCLUDED.consent_proof_id
            ELSE contact_profile.consent_proof_id
        END,
        updated_at       = :now
    """
)

_INSERT_QUARANTINE = text(
    """
    INSERT INTO contact_quarantine (quarantine_id, tenant, rejected_at, contact_id, reason, raw)
    VALUES (:id, :tenant, :now, NULL, :reason, CAST(:raw AS jsonb))
    """
)


async def _resolve_provider(
    session: AsyncSession, tenant: str, domain: str
) -> tuple[int, int, str | None]:
    """Return (mailbox_provider, reputation_group, unknown_domain_or_None).

    domain_provider wins when it has a row — it carries MX-resolved and manual
    corrections that the static seed table in isp.py does not.
    """
    row = (
        await session.execute(
            _LOOKUP_DOMAIN_PROVIDER, {"tenant": tenant, "domain": domain}
        )
    ).mappings().one_or_none()
    if row is not None:
        return int(row["mailbox_provider"]), int(row["reputation_group"]), None

    mailbox, reputation = classify_domain_two_axis(domain)
    mp = MAILBOX_PROVIDER_CODES.get(mailbox, MAILBOX_PROVIDER_CODES["other"])
    rg = REPUTATION_GROUP_CODES.get(reputation, REPUTATION_GROUP_CODES["other"])
    unknown = domain if (mailbox == "other" and reputation == "other") else None
    return mp, rg, unknown


async def ingest_record(
    session: AsyncSession,
    *,
    tenant: str,
    vertical: str,
    record: NormalizedRecord,
    now: datetime,
) -> tuple[str, str | None]:
    """Write recipients + consent_proof + contact_profile for one record.

    Returns (status, unknown_domain) where status is "created" or "updated"
    and unknown_domain is a domain needing an MX lookup, or None.
    """
    row = (
        await session.execute(
            _UPSERT_RECIPIENT,
            {
                "id": str(uuid7()),
                "tenant": tenant,
                "email": record.email,
                "external_id": record.external_id,
                "vertical": record.vertical or vertical,
                "now": now,
                "consent_source": record.consent_source,
                "consent_at": record.consent.captured_at,
                "attributes": json.dumps(record.attributes),
            },
        )
    ).one()
    contact_id = row.id
    status = "created" if row.created else "updated"

    text_hash = disclosure_hash(record.consent.text)
    proof_params = {
        "tenant": tenant,
        "contact_id": str(contact_id),
        "captured_at": record.consent.captured_at,
        "hash": text_hash,
    }
    proof_id = (
        await session.execute(
            _INSERT_CONSENT_PROOF,
            {
                **proof_params,
                "id": str(uuid7()),
                "source_url": record.consent.source_url,
                "form_id": record.consent.form_id,
                "ip": record.consent.ip,
                "user_agent": record.consent.user_agent,
                "channels": list(record.consent.channels),
                "raw": json.dumps(
                    {
                        "disclosure_text": record.consent.text,
                        "attributes": record.attributes,
                    },
                    default=str,
                ),
            },
        )
    ).scalar_one_or_none()
    if proof_id is None:
        # Dedup hit: this exact consent event is already on file.
        proof_id = (
            await session.execute(_SELECT_EXISTING_PROOF, proof_params)
        ).scalar_one()

    suppressed = (
        await session.execute(
            _IS_SUPPRESSED, {"tenant": tenant, "email": record.email}
        )
    ).scalar_one()

    domain = record.email.rsplit("@", 1)[1]
    mp, rg, unknown_domain = await _resolve_provider(session, tenant, domain)

    await session.execute(
        _UPSERT_PROFILE,
        {
            "contact_id": str(contact_id),
            "tenant": tenant,
            "channel": _CHANNEL_EMAIL,
            "email_domain": domain,
            "mailbox_provider": mp,
            "reputation_group": rg,
            "consent_state": _CONSENT_OPTED_OUT if suppressed else _CONSENT_OPTED_IN,
            "consent_at": record.consent.captured_at,
            "consent_proof_id": str(proof_id),
            "suppressed_at": now if suppressed else None,
            "suppress_reason": _SUPPRESS_REASON_UNSUBSCRIBE if suppressed else None,
            "now": now,
        },
    )
    return status, unknown_domain


async def quarantine_row(
    session: AsyncSession,
    *,
    tenant: str,
    reason: str,
    raw_row: dict,
    now: datetime,
) -> None:
    """Record a row rejected for want of consent (SPEC-segmentation §9)."""
    await session.execute(
        _INSERT_QUARANTINE,
        {
            "id": str(uuid7()),
            "tenant": tenant,
            "now": now,
            "reason": reason[:500],
            "raw": json.dumps(raw_row, default=str),
        },
    )


async def flush_unknown_domains(
    tenant: str, domains: set[str], *, concurrency: int = 50
) -> None:
    """Enqueue MX lookups and await them.

    enqueue_mx_lookup fires a background task and returns. That is correct for
    a long-lived worker, but a CLI's event loop closes when main() returns and
    cancels those tasks, losing the classification. Bounded and awaited here.
    """
    if not domains:
        return

    from app.workers.domain_provider_mx_worker import enqueue_mx_lookup

    semaphore = asyncio.Semaphore(concurrency)

    async def _one(domain: str) -> None:
        async with semaphore:
            await enqueue_mx_lookup(domain, tenant)

    await asyncio.gather(*(_one(d) for d in sorted(domains)), return_exceptions=True)
    # enqueue_mx_lookup spawns the DNS work with create_task; drain those too.
    pending = [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and "_run_mx_lookup" in repr(t)
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    log.info("record_ingest.mx_enqueued", tenant=tenant, domains=len(domains))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/services/test_record_ingest.py -v`
Expected: PASS (7 tests).

If `test_suppressed_contact_is_not_resurrected` fails because `LEAST` picked the wrong direction, remember `consent_state` 1 = opted in, 2 = opted out, so `LEAST` keeps opted-in **unless** the incoming value is 1 and the stored one is 2 — verify the stored value stays 2. That is the assertion.

- [ ] **Step 6: Commit**

```bash
git add app/services/record_ingest.py tests/services/test_record_ingest.py tests/services/conftest.py
git commit -m "feat: record ingest service with suppression and consent guards"
```

---

### Task 8: the CLI

**Files:**
- Create: `app/cli/__init__.py`, `app/cli/load_records.py`
- Test: `tests/cli/test_load_records.py`
- Modify: `.gitignore`
- Modify: `CLAUDE.md` (Commands section)

**Interfaces:**
- Consumes: `load_tenant_mapping` (Task 5), `map_row` (Task 6), `ingest_record` / `quarantine_row` / `flush_unknown_domains` / `IngestCounters` (Task 7), `enroll_recipient` from `app/api/_enrollment.py`.
- Produces:
  - `read_rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]` — 1-indexed row number and row dict, CSV or JSONL by suffix
  - `async def run(args: argparse.Namespace) -> int` — returns the process exit code
  - `def main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/cli/test_load_records.py
"""End-to-end CLI tests over a temporary CSV."""

from __future__ import annotations

import csv
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.cli.load_records import main, read_rows
from app.core.db import async_session_factory

_TENANT = "otto"

_HEADERS = [
    "email", "user_id", "first_name", "last_name", "address", "city", "state",
    "zip", "phone", "vehicle_1_year", "vehicle_1_make", "vehicle_1_model",
    "tcpa_text", "tcpa_consent_at", "consent_url", "lead_id", "consent_ip",
    "consent_ua",
]


def _row(email: str, **overrides) -> dict[str, str]:
    row = {
        "email": email, "user_id": "otto-1", "first_name": "Shawn",
        "last_name": "Estes", "address": "100 Congress Ave", "city": "Austin",
        "state": "TX", "zip": "78701", "phone": "5125551234",
        "vehicle_1_year": "2019", "vehicle_1_make": "Toyota",
        "vehicle_1_model": "Camry",
        "tcpa_text": "By submitting you agree to receive emails.",
        "tcpa_consent_at": "2026-01-15T10:30:00Z",
        "consent_url": "https://otto.example/quote", "lead_id": "form-7",
        "consent_ip": "203.0.113.9", "consent_ua": "Mozilla/5.0",
    }
    row.update(overrides)
    return row


def _write_csv(path: Path, rows: list[dict[str, str]], *, encoding: str = "utf-8") -> None:
    with path.open("w", newline="", encoding=encoding) as fh:
        writer = csv.DictWriter(fh, fieldnames=_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def test_read_rows_handles_bom_and_embedded_newlines(tmp_path: Path):
    path = tmp_path / "leads.csv"
    _write_csv(
        path,
        [_row("bom@cadencetests.io", tcpa_text="Line one\nLine two")],
        encoding="utf-8-sig",
    )
    rows = list(read_rows(path))
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1]["email"] == "bom@cadencetests.io"
    assert rows[0][1]["tcpa_text"] == "Line one\nLine two"


@pytest.mark.asyncio
async def test_cli_loads_quarantines_and_rejects(tmp_path: Path, capsys):
    good = f"good-{uuid.uuid4()}@cadencetests.io"
    noconsent = f"noconsent-{uuid.uuid4()}@cadencetests.io"
    path = tmp_path / "leads.csv"
    errors_out = tmp_path / "rejects.csv"
    _write_csv(
        path,
        [
            _row(good),
            _row(noconsent, tcpa_text=""),
            _row("not-an-email"),
        ],
    )

    code = main(
        [
            "--tenant", _TENANT, "--file", str(path), "--vertical", "auto",
            "--errors-out", str(errors_out), "--max-error-rate", "1.0",
        ]
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "loaded=1" in out
    assert "quarantined=1" in out
    assert "rejected=1" in out

    assert errors_out.exists()
    reject_rows = list(csv.DictReader(errors_out.open(newline="", encoding="utf-8")))
    assert len(reject_rows) == 2  # the quarantined row is reported too

    async with async_session_factory() as s:
        loaded = (
            await s.execute(
                text("SELECT count(*) FROM recipients WHERE tenant=:t AND email=:e"),
                {"t": _TENANT, "e": good},
            )
        ).scalar_one()
        quarantined = (
            await s.execute(
                text(
                    "SELECT count(*) FROM contact_quarantine"
                    " WHERE tenant=:t AND raw->>'email' = :e"
                ),
                {"t": _TENANT, "e": noconsent},
            )
        ).scalar_one()
        never_created = (
            await s.execute(
                text("SELECT count(*) FROM recipients WHERE tenant=:t AND email=:e"),
                {"t": _TENANT, "e": noconsent},
            )
        ).scalar_one()
    assert (loaded, quarantined, never_created) == (1, 1, 0)


@pytest.mark.asyncio
async def test_dry_run_touches_no_db(tmp_path: Path):
    email = f"dry-{uuid.uuid4()}@cadencetests.io"
    path = tmp_path / "leads.csv"
    _write_csv(path, [_row(email)])

    code = main(
        [
            "--tenant", _TENANT, "--file", str(path), "--vertical", "auto",
            "--errors-out", str(tmp_path / "r.csv"), "--dry-run",
        ]
    )
    assert code == 0

    async with async_session_factory() as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM recipients WHERE tenant=:t AND email=:e"),
                {"t": _TENANT, "e": email},
            )
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_exit_code_2_when_error_rate_exceeded(tmp_path: Path):
    path = tmp_path / "leads.csv"
    _write_csv(path, [_row("bad-one"), _row("bad-two"), _row(f"ok-{uuid.uuid4()}@cadencetests.io")])
    code = main(
        [
            "--tenant", _TENANT, "--file", str(path), "--vertical", "auto",
            "--errors-out", str(tmp_path / "r.csv"), "--max-error-rate", "0.25",
        ]
    )
    assert code == 2


@pytest.mark.asyncio
async def test_duplicate_email_in_one_batch_last_wins(tmp_path: Path):
    email = f"dupe-{uuid.uuid4()}@cadencetests.io"
    path = tmp_path / "leads.csv"
    _write_csv(
        path,
        [_row(email, city="Austin"), _row(email, city="Dallas")],
    )
    code = main(
        [
            "--tenant", _TENANT, "--file", str(path), "--vertical", "auto",
            "--errors-out", str(tmp_path / "r.csv"),
        ]
    )
    assert code == 0
    async with async_session_factory() as s:
        attrs = (
            await s.execute(
                text("SELECT attributes FROM recipients WHERE tenant=:t AND email=:e"),
                {"t": _TENANT, "e": email},
            )
        ).scalar_one()
    assert attrs["city"] == "Dallas"
```

Copy `tests/services/conftest.py` (from Task 7) to `tests/cli/conftest.py` so the same cleanup applies.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cli/test_load_records.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.cli'`.

- [ ] **Step 3: Write the CLI**

```python
# app/cli/__init__.py
"""Operator command-line entry points."""
```

```python
# app/cli/load_records.py
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
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from app.api._enrollment import enroll_recipient
from app.core.db import async_session_factory, engine
from app.domain.record_mapping import (
    OUTCOME_OK,
    OUTCOME_QUARANTINE,
    map_row,
)
from app.domain.segment import matches as segment_matches
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


async def _enroll(session, tenant: str, contact_id, attributes: dict, vertical: str, now) -> int:
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

        async def _process(rows: list[tuple[int, dict[str, Any]]], session) -> None:
            now = datetime.now(timezone.utc)
            for row_number, raw in rows:
                mapped = map_row(raw, mapping)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cli/test_load_records.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Ignore the rejects directory**

Add to `.gitignore`:

```
import-logs/
*-rejects.csv
```

- [ ] **Step 6: Document the command**

Add to the Commands section of `CLAUDE.md`:

````markdown
```bash
# Bulk-load tenant records (rejects file holds PII — it is gitignored)
uv run python -m app.cli.load_records --tenant otto --file leads.csv --vertical auto
uv run python -m app.cli.load_records --tenant otto --file leads.csv --vertical auto --dry-run
```
````

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS. Investigate any pre-existing failure before assuming it is unrelated.

- [ ] **Step 8: Commit**

```bash
git add app/cli tests/cli .gitignore CLAUDE.md
git commit -m "feat: bulk record loader CLI"
```

---

### Task 9: batch ingest endpoint

**Files:**
- Create: `app/schemas/records.py`
- Modify: `app/api/ingest.py` (append the route)
- Test: `tests/api/test_records_batch.py`

**Interfaces:**
- Consumes: everything from Tasks 5–7.
- Produces: `POST /api/ingest/records/batch`.

Mounted under the existing authenticated `/api` router group in `app/main.py:45-49` — the router already carries `Depends(require_api_key)`. Do **not** mount it under the bare `/ingest` prefix; that is `origin_platform`'s deliberately unauthenticated private-network route.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_records_batch.py
"""POST /api/ingest/records/batch."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.db import async_session_factory

_GOOD_CONSENT = {
    "tcpa_text": "By submitting you agree to receive emails.",
    "tcpa_consent_at": "2026-01-15T10:30:00Z",
}


def _record(email: str, **overrides) -> dict:
    row = {
        "email": email, "user_id": "otto-1", "first_name": "Shawn",
        "last_name": "Estes", "address": "100 Congress Ave", "city": "Austin",
        "state": "TX", "zip": "78701", "phone": "5125551234",
        **_GOOD_CONSENT,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_batch_requires_auth(client):
    resp = await client.post(
        "/api/ingest/records/batch",
        json={"tenant": "otto", "vertical": "auto", "records": []},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_batch_reports_per_row_status(client, auth_headers):
    good = f"api-good-{uuid.uuid4()}@cadencetests.io"
    noconsent = f"api-nc-{uuid.uuid4()}@cadencetests.io"
    resp = await client.post(
        "/api/ingest/records/batch",
        headers=auth_headers,
        json={
            "tenant": "otto",
            "vertical": "auto",
            "records": [
                _record(good),
                _record(noconsent, tcpa_text=""),
                _record("not-an-email"),
            ],
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["status"] for r in results] == ["created", "quarantined", "rejected"]
    assert results[0]["index"] == 0
    assert results[2]["errors"]

    async with async_session_factory() as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM recipients WHERE tenant='otto' AND email=:e"),
                {"e": good},
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_batch_size_is_capped(client, auth_headers):
    resp = await client.post(
        "/api/ingest/records/batch",
        headers=auth_headers,
        json={
            "tenant": "otto",
            "vertical": "auto",
            "records": [_record(f"cap-{n}@cadencetests.io") for n in range(201)],
        },
    )
    assert resp.status_code == 422
```

Copy `tests/services/conftest.py` to `tests/api/conftest.py` **only if** `tests/api/` does not already have one; if it does, add the `contact_profile` / `consent_proof` / `contact_quarantine` cleanup statements into the existing file rather than replacing it.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_records_batch.py -v`
Expected: FAIL — 404 on the route.

- [ ] **Step 3: Write the schema**

```python
# app/schemas/records.py
from typing import Any, Literal

from pydantic import BaseModel, Field


class RecordBatchIn(BaseModel):
    tenant: str = "otto"
    vertical: Literal["auto", "home"]
    enroll: bool = False
    # Three DB writes per record: 200 keeps the request inside gateway timeouts.
    records: list[dict[str, Any]] = Field(max_length=200)


class RecordResult(BaseModel):
    index: int
    status: Literal["created", "updated", "quarantined", "rejected"]
    errors: list[str] = []


class RecordBatchOut(BaseModel):
    results: list[RecordResult]
    counters: dict[str, int]
```

- [ ] **Step 4: Add the route to `app/api/ingest.py`**

```python
# append to app/api/ingest.py
@router.post("/ingest/records/batch", response_model=RecordBatchOut)
async def ingest_records_batch(
    payload: RecordBatchIn,
    db: AsyncSession = Depends(get_db),
) -> RecordBatchOut:
    """Bulk record ingest. Same service the CLI uses (spec §11)."""
    mapping = load_tenant_mapping(payload.tenant)
    now = datetime.now(timezone.utc)
    counters = IngestCounters()
    results: list[RecordResult] = []
    unknown_domains: set[str] = set()

    for index, raw in enumerate(payload.records):
        counters.read += 1
        mapped = map_row(raw, mapping)
        if mapped.outcome == OUTCOME_OK:
            status, unknown = await ingest_record(
                db,
                tenant=payload.tenant,
                vertical=payload.vertical,
                record=mapped.record,
                now=now,
            )
            if unknown:
                unknown_domains.add(unknown)
            if status == "created":
                counters.loaded += 1
            else:
                counters.updated += 1
            results.append(RecordResult(index=index, status=status))
        elif mapped.outcome == OUTCOME_QUARANTINE:
            await quarantine_row(
                db,
                tenant=payload.tenant,
                reason="; ".join(mapped.errors),
                raw_row=raw,
                now=now,
            )
            counters.quarantined += 1
            results.append(
                RecordResult(index=index, status="quarantined", errors=list(mapped.errors))
            )
        else:
            counters.rejected += 1
            results.append(
                RecordResult(index=index, status="rejected", errors=list(mapped.errors))
            )

    await db.commit()
    await flush_unknown_domains(payload.tenant, unknown_domains)
    return RecordBatchOut(results=results, counters=counters.as_dict())
```

Add the imports at the top of `app/api/ingest.py`:

```python
from app.domain.record_mapping import OUTCOME_OK, OUTCOME_QUARANTINE, map_row
from app.schemas.records import RecordBatchIn, RecordBatchOut, RecordResult
from app.services.record_ingest import (
    IngestCounters,
    flush_unknown_domains,
    ingest_record,
    quarantine_row,
)
from app.services.tenant_mapping import load_tenant_mapping
```

`payload.enroll` is accepted but not yet acted on — enrollment through the API is gated by the same threshold as the CLI and is deliberately deferred. Raise `HTTPException(400, "enroll is not supported on the batch endpoint yet")` when it is `True`, so the flag never silently no-ops.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/api/test_records_batch.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/schemas/records.py app/api/ingest.py tests/api/test_records_batch.py
git commit -m "feat: bulk record batch ingest endpoint"
```

---

## Verification

Before reporting the work complete:

- [ ] `docker compose -f docker-compose.test.yml up -d --wait`
- [ ] `uv run pytest -q` — full suite green, with the pass count noted
- [ ] `uv run alembic upgrade head` on a fresh test DB succeeds
- [ ] A manual `--dry-run` over a hand-written 3-row CSV prints the expected counters
- [ ] `git log --oneline` shows one commit per task, none pushed

## Out of scope, recorded in the spec (§13)

1. `domain_provider` → `contact_profile` reclassification sync (SPEC-segmentation case 12).
2. `app/api/workflows.py:242` calls `compile_segment` with the wrong signature; that endpoint raises `TypeError`.
3. SMS consent (`channels: [1, 2]`).
4. Hoisting the `attributes.*` correlated subquery to a JOIN if materialize latency regresses.
