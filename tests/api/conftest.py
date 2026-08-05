"""Local conftest for tests/api/.

Some tests here now seed contact_profile (and its required consent_proof FK)
directly, since compile_segment targets contact_profile rather than
recipients (see tests/api/_contact_profile_seed.py). Those rows must be
purged before the global ``_clean_test_rows`` fixture in tests/conftest.py
deletes the underlying recipients rows, or the delete fails on the FK.

Reuses the purge helper already written for tests/workers/ rather than
duplicating it.
"""
from __future__ import annotations

import pytest_asyncio

from tests.workers.conftest import _purge_segmentation_rows


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _session_purge_segmentation_api(_bootstrap_test_db):
    """Session-scoped one-shot purge, mirroring tests/workers/conftest.py."""
    await _purge_segmentation_rows()
    yield


@pytest_asyncio.fixture(autouse=True)
async def _per_test_purge_segmentation_api():
    """Per-test teardown: remove segmentation rows before _clean_test_rows."""
    yield
    await _purge_segmentation_rows()
