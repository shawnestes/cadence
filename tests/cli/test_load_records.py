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
