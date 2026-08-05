"""Local conftest for tests/workers/.

Adds cleanup of segmentation tables (contact_profile, consent_proof,
segment_member, segment_definition) to prevent FK violations when the
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
"""
from __future__ import annotations

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
