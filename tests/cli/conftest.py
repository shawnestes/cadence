"""Local conftest for tests/cli/.

Copied from tests/services/conftest.py (Task 7): adds cleanup of
segmentation tables (contact_profile, consent_proof, segment_member,
segment_definition, contact_quarantine) to prevent FK violations when the
global _clean_test_rows fixture in tests/conftest.py deletes recipients.

Strategy
--------
A session-scoped async autouse fixture runs ONCE before any function-scoped
fixtures.  It removes orphaned consent_proof rows from prior failed test runs
so that the first _clean_test_rows pre-purge (which deletes recipients) does
not encounter FK violations.

A function-scoped async autouse fixture runs teardown BEFORE the parent-level
_clean_test_rows teardown (local-conftest teardown is always child-first).
This ensures consent_proof rows are deleted before _clean_test_rows deletes
the corresponding recipient rows.

Also purges the domain_provider placeholder row(s) the CLI's real (unmocked)
flush_unknown_domains call inserts for the shared cadencetests.io test
domain. Without this, that row survives into tests/services/test_record_ingest.py,
which asserts cadencetests.io is *unclassified* — a real cross-file pollution
this suite hit before the cleanup was added here.

flush_unknown_domains is additionally stubbed to a no-op by default (see
_stub_flush_unknown_domains below) so tests don't incidentally hit the real
DNS-enqueue / domain_provider-insert path at all. The domain_provider purge
above stays as a backstop for any test that explicitly re-enables the real
flush and needs it exercised end to end.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.db import async_session_factory

_TENANT = "otto"


async def _purge_segmentation_rows() -> None:
    """Delete all segmentation rows for test contacts (FK-safe order).

    Handles both live test recipients AND orphaned rows from prior failed runs
    (consent_proof rows whose FK target in recipients was already removed).
    """
    async with async_session_factory() as s:
        await s.execute(
            text(
                "DELETE FROM segment_member WHERE tenant = :t "
                "AND segment_id IN ("
                "  SELECT segment_id FROM segment_definition "
                "  WHERE tenant = :t AND key LIKE 'test-%'"
                ")"
            ),
            {"t": _TENANT},
        )
        await s.execute(
            text(
                "DELETE FROM segment_definition WHERE tenant = :t AND key LIKE 'test-%'"
            ),
            {"t": _TENANT},
        )
        await s.execute(
            text(
                "DELETE FROM contact_profile WHERE tenant = :t "
                "AND contact_id IN ("
                "  SELECT id FROM recipients WHERE email LIKE :pat"
                ")"
            ),
            {"t": _TENANT, "pat": "%@cadencetests.io"},
        )
        # Delete contact_activity_daily for test contacts before recipients
        # (FK: contact_activity_daily.contact_id → recipients.id).
        await s.execute(
            text(
                "DELETE FROM contact_activity_daily WHERE tenant = :t "
                "AND contact_id IN ("
                "  SELECT id FROM recipients WHERE email LIKE :pat"
                ")"
            ),
            {"t": _TENANT, "pat": "%@cadencetests.io"},
        )
        # Delete consent_proof for live test recipients AND orphaned rows whose
        # recipient no longer exists (stale rows from prior failed runs).
        await s.execute(
            text(
                "DELETE FROM consent_proof cp "
                "WHERE cp.tenant = :t "
                "AND ("
                "  cp.contact_id IN (SELECT id FROM recipients WHERE email LIKE :pat)"
                "  OR NOT EXISTS (SELECT 1 FROM recipients r WHERE r.id = cp.contact_id)"
                ")"
            ),
            {"t": _TENANT, "pat": "%@cadencetests.io"},
        )
        await s.execute(
            text("DELETE FROM contact_quarantine WHERE tenant = :t AND raw->>'email' LIKE :pat"),
            {"t": _TENANT, "pat": "%@cadencetests.io"},
        )
        await s.execute(
            text("DELETE FROM domain_provider WHERE tenant = :t AND domain = :d"),
            {"t": _TENANT, "d": "cadencetests.io"},
        )
        await s.commit()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _session_purge_segmentation(_bootstrap_test_db):
    """Session-scoped one-shot purge.

    Runs before any function-scoped fixtures and after the DB is bootstrapped,
    clearing orphaned consent_proof rows from prior failed runs.
    """
    await _purge_segmentation_rows()
    yield


@pytest_asyncio.fixture(autouse=True)
async def _per_test_purge_segmentation():
    """Per-test teardown: remove segmentation rows before _clean_test_rows.

    The yield is intentional — we only need the teardown phase here.
    Setup-phase cleanup is handled by _session_purge_segmentation (once per
    session) and by the previous test's teardown on subsequent tests.

    Local-conftest (child-conftest) fixtures tear down BEFORE parent-conftest
    fixtures, so this runs before _clean_test_rows and consent_proof is removed
    before the global fixture attempts to delete recipients.
    """
    yield
    await _purge_segmentation_rows()


@pytest.fixture(autouse=True)
def _stub_flush_unknown_domains(monkeypatch):
    """Default CLI tests away from the real MX-enqueue/DNS path.

    app.cli.load_records.flush_unknown_domains performs a real (unmocked)
    DNS-backed domain_provider insert. Most CLI tests have nothing to do with
    that path and shouldn't incidentally hit real DNS or mutate
    domain_provider; tests that genuinely want to exercise it should
    monkeypatch it back explicitly (a real dependency injection point, not a
    global) and clean up after themselves.
    """
    import app.cli.load_records as load_records_module

    async def _noop(tenant: str, domains: set[str], *, concurrency: int = 50) -> None:
        return None

    monkeypatch.setattr(load_records_module, "flush_unknown_domains", _noop)
