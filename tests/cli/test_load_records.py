"""End-to-end CLI tests over a temporary CSV.

The async tests call ``run()`` directly with a hand-built argparse.Namespace
rather than going through ``main()``. ``main()`` wraps ``run()`` in its own
``asyncio.run()`` call, which cannot be invoked from inside a running event
loop (these tests already run on pytest-asyncio's session-scoped loop) and,
when routed through a helper thread instead, was observed to leave the test
process hanging at teardown. Calling ``run()`` directly sidesteps both:
no nested loop, no extra thread, no cross-loop connection-pool sharing.
``main()``'s own responsibility — argv parsing plus the asyncio.run/dispose
wrapper — is intentionally thin and is not separately covered here.
"""

from __future__ import annotations

import argparse
import csv
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.cli.load_records import read_rows, run
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


def _args(**overrides) -> argparse.Namespace:
    """Build the Namespace _parse_args would produce, for calling run() directly."""
    defaults = dict(
        tenant=_TENANT,
        file="",
        vertical="auto",
        batch_size=1000,
        encoding="auto",
        errors_out="",
        limit=0,
        max_error_rate=0.25,
        enroll=False,
        yes=False,
        dry_run=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


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

    code = await run(
        _args(file=str(path), errors_out=str(errors_out), max_error_rate=1.0)
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

    code = await run(
        _args(file=str(path), errors_out=str(tmp_path / "r.csv"), dry_run=True)
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
    code = await run(
        _args(file=str(path), errors_out=str(tmp_path / "r.csv"), max_error_rate=0.25)
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
    code = await run(_args(file=str(path), errors_out=str(tmp_path / "r.csv")))
    assert code == 0
    async with async_session_factory() as s:
        attrs = (
            await s.execute(
                text("SELECT attributes FROM recipients WHERE tenant=:t AND email=:e"),
                {"t": _TENANT, "e": email},
            )
        ).scalar_one()
    assert attrs["city"] == "Dallas"


async def _load_and_get_attributes(tmp_path: Path, path: Path, email: str, **arg_overrides):
    code = await run(
        _args(
            file=str(path),
            errors_out=str(tmp_path / "r.csv"),
            max_error_rate=1.0,
            **arg_overrides,
        )
    )
    assert code == 0
    async with async_session_factory() as s:
        return (
            await s.execute(
                text("SELECT attributes FROM recipients WHERE tenant=:t AND email=:e"),
                {"t": _TENANT, "e": email},
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_cp1252_file_auto_detected_stores_correct_accented_names(tmp_path: Path):
    """A cp1252 file (no BOM) with no --encoding flag must auto-detect as
    cp1252 (utf-8-sig probing fails on the accented bytes) and store the
    real characters, not corrupt or reject them.
    """
    email = f"cp1252-{uuid.uuid4()}@cadencetests.io"
    path = tmp_path / "leads.csv"
    _write_csv(
        path,
        [_row(email, first_name="Renée", last_name="Beauséjour")],
        encoding="cp1252",
    )
    attrs = await _load_and_get_attributes(tmp_path, path, email)
    assert attrs["first_name"] == "Renée"
    assert attrs["last_name"] == "Beauséjour"


@pytest.mark.asyncio
async def test_utf8_bom_file_auto_detected_stores_correct_accented_names(tmp_path: Path):
    email = f"utf8bom-{uuid.uuid4()}@cadencetests.io"
    path = tmp_path / "leads.csv"
    _write_csv(
        path,
        [_row(email, first_name="Renée", last_name="Beauséjour")],
        encoding="utf-8-sig",
    )
    attrs = await _load_and_get_attributes(tmp_path, path, email)
    assert attrs["first_name"] == "Renée"
    assert attrs["last_name"] == "Beauséjour"


@pytest.mark.asyncio
async def test_plain_utf8_file_auto_detected_stores_correct_accented_names(tmp_path: Path):
    email = f"utf8plain-{uuid.uuid4()}@cadencetests.io"
    path = tmp_path / "leads.csv"
    _write_csv(
        path,
        [_row(email, first_name="Renée", last_name="Beauséjour")],
        encoding="utf-8",
    )
    attrs = await _load_and_get_attributes(tmp_path, path, email)
    assert attrs["first_name"] == "Renée"
    assert attrs["last_name"] == "Beauséjour"


@pytest.mark.asyncio
async def test_explicit_encoding_flag_used_verbatim_no_probing(tmp_path: Path):
    """--encoding cp1252 must be used as given, with no auto-detection."""
    email = f"explicitcp1252-{uuid.uuid4()}@cadencetests.io"
    path = tmp_path / "leads.csv"
    _write_csv(
        path,
        [_row(email, first_name="Renée", last_name="Beauséjour")],
        encoding="cp1252",
    )
    attrs = await _load_and_get_attributes(tmp_path, path, email, encoding="cp1252")
    assert attrs["first_name"] == "Renée"
    assert attrs["last_name"] == "Beauséjour"


@pytest.mark.asyncio
async def test_invalid_byte_past_probe_window_aborts_loudly(tmp_path: Path, capsys):
    """A file that's valid UTF-8 for well past the 1 MiB auto-detect probe
    and then contains a byte that can't decode must abort the whole run
    loudly — naming the encoding and location — rather than mangle that row
    or silently keep going under a different codec.
    """
    path = tmp_path / "leads.csv"
    padding = "A" * 2000
    # ~700 padded rows comfortably exceeds the 1 MiB probe window before the
    # bad byte (written separately below) ever appears.
    filler_rows = [
        _row(f"filler{i}-{uuid.uuid4()}@cadencetests.io", tcpa_text=padding)
        for i in range(700)
    ]
    _write_csv(path, filler_rows)
    assert path.stat().st_size > 1024 * 1024

    bad_email = f"bad-{uuid.uuid4()}@cadencetests.io"
    # Append a row containing a raw cp1252 accented byte directly as bytes:
    # invalid as a UTF-8 continuation, and past the probe window, so the
    # auto-detected "utf-8-sig" from the clean first 1 MiB will choke on it.
    bad_line = (
        f"{bad_email},otto-1,Ren\xe9e,Estes,100 Congress Ave,Austin,TX,78701,"
        "5125551234,2019,Toyota,Camry,consent text,2026-01-15T10:30:00Z,"
        "https://otto.example/quote,form-7,203.0.113.9,Mozilla/5.0\r\n"
    ).encode("cp1252")
    with path.open("ab") as fh:
        fh.write(bad_line)

    code = await run(
        _args(file=str(path), errors_out=str(tmp_path / "r.csv"), max_error_rate=1.0)
    )
    assert code != 0

    err = capsys.readouterr().err
    assert "utf-8-sig" in err
    assert "row" in err

    async with async_session_factory() as s:
        mangled = (
            await s.execute(
                text(
                    "SELECT count(*) FROM recipients WHERE tenant=:t AND email=:e"
                ),
                {"t": _TENANT, "e": bad_email},
            )
        ).scalar_one()
    assert mangled == 0


@pytest.mark.asyncio
async def test_batch_failure_rolls_back_and_retries_rows_individually(
    tmp_path: Path, monkeypatch
):
    """A statement-level failure partway through a batch must not lose the
    other rows in that batch. Force a genuine failure (not a duplicate-key
    upsert, which never raises) by making ingest_record blow up for one
    specific email, and confirm: the other two rows in the same batch are
    still committed, the bad row is counted rejected and lands in the
    rejects file, and the process exits normally.
    """
    good1 = f"batchgood1-{uuid.uuid4()}@cadencetests.io"
    bad = f"batchbad-{uuid.uuid4()}@cadencetests.io"
    good2 = f"batchgood2-{uuid.uuid4()}@cadencetests.io"
    path = tmp_path / "leads.csv"
    errors_out = tmp_path / "r.csv"
    _write_csv(path, [_row(good1), _row(bad), _row(good2)])

    import app.cli.load_records as load_records_module

    real_ingest_record = load_records_module.ingest_record

    async def _flaky_ingest_record(session, *, tenant, vertical, record, now):
        if record.email == bad:
            raise RuntimeError("simulated statement failure")
        return await real_ingest_record(
            session, tenant=tenant, vertical=vertical, record=record, now=now
        )

    monkeypatch.setattr(load_records_module, "ingest_record", _flaky_ingest_record)

    code = await run(
        _args(file=str(path), errors_out=str(errors_out), max_error_rate=1.0)
    )
    assert code == 0

    async with async_session_factory() as s:
        good1_count = (
            await s.execute(
                text("SELECT count(*) FROM recipients WHERE tenant=:t AND email=:e"),
                {"t": _TENANT, "e": good1},
            )
        ).scalar_one()
        good2_count = (
            await s.execute(
                text("SELECT count(*) FROM recipients WHERE tenant=:t AND email=:e"),
                {"t": _TENANT, "e": good2},
            )
        ).scalar_one()
        bad_count = (
            await s.execute(
                text("SELECT count(*) FROM recipients WHERE tenant=:t AND email=:e"),
                {"t": _TENANT, "e": bad},
            )
        ).scalar_one()
    assert (good1_count, good2_count, bad_count) == (1, 1, 0)

    reject_rows = list(csv.DictReader(errors_out.open(newline="", encoding="utf-8")))
    error_rows = [r for r in reject_rows if r["outcome"] == "error"]
    assert len(error_rows) == 1
    assert bad in error_rows[0]["raw"]
