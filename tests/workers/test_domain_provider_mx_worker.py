"""Unit tests for the MX classifier and enqueue idempotency.

Covers:
  - _classify_mx_host: pure function, no I/O.
  - enqueue_mx_lookup: mocked DB session; verifies INSERT … ON CONFLICT SQL is
    issued without touching a real database or DNS.
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.domain_provider_mx_worker import _classify_mx_host, enqueue_mx_lookup


def _create_task_and_close(coro: object) -> MagicMock:
    """Mock for asyncio.create_task that closes the coroutine to silence
    'coroutine never awaited' warnings without actually running the DNS lookup.
    """
    if asyncio.iscoroutine(coro):
        coro.close()
    return MagicMock()


# ---------------------------------------------------------------------------
# _classify_mx_host — pure function tests
# ---------------------------------------------------------------------------


class TestClassifyMxHost:
    """Tests for _classify_mx_host."""

    # --- Google Workspace / Gmail infrastructure ---

    def test_google_aspmx(self) -> None:
        assert _classify_mx_host("aspmx.l.google.com") == ("corporate", "gmail")

    def test_google_gmail_smtp_in(self) -> None:
        assert _classify_mx_host("gmail-smtp-in.l.google.com") == ("corporate", "gmail")

    # --- Microsoft 365 / Exchange Online Protection ---

    def test_microsoft_outlook_protection(self) -> None:
        assert _classify_mx_host("acme-com.mail.protection.outlook.com") == (
            "corporate",
            "microsoft",
        )

    # --- Yahoo MX infrastructure ---

    def test_yahoo_am0_yahoodns(self) -> None:
        assert _classify_mx_host("mta5.am0.yahoodns.net") == ("other", "yahoo")

    # --- Unknown / fallthrough ---

    def test_unknown_domain(self) -> None:
        assert _classify_mx_host("mx.someunknown.example") == ("other", "other")

    # --- Normalization ---

    def test_leading_trailing_dot_normalized(self) -> None:
        """A trailing dot (as returned by some DNS libraries) must be handled."""
        assert _classify_mx_host("aspmx.l.google.com.") == ("corporate", "gmail")

    def test_uppercase_normalized(self) -> None:
        """DNS hostnames are case-insensitive; the classifier must normalize."""
        assert _classify_mx_host("ASPMX.L.GOOGLE.COM") == ("corporate", "gmail")

    def test_uppercase_microsoft(self) -> None:
        assert _classify_mx_host("ACME-COM.MAIL.PROTECTION.OUTLOOK.COM") == (
            "corporate",
            "microsoft",
        )

    def test_uppercase_yahoo(self) -> None:
        assert _classify_mx_host("MTA5.AM0.YAHOODNS.NET") == ("other", "yahoo")

    def test_mixed_case_with_trailing_dot(self) -> None:
        assert _classify_mx_host("Gmail-Smtp-In.L.Google.Com.") == ("corporate", "gmail")

    # --- Edge: googlemail.com suffix ---

    def test_googlemail_suffix(self) -> None:
        assert _classify_mx_host("smtp-in.googlemail.com") == ("corporate", "gmail")

    # --- Edge: different yahoodns subdomain structures ---

    def test_yahoodns_different_subdomain(self) -> None:
        assert _classify_mx_host("mta7.am0.yahoodns.net") == ("other", "yahoo")

    # --- Edge: partial match must not fire ---

    def test_partial_google_does_not_match(self) -> None:
        """'notgoogle.com' should not be classified as google."""
        assert _classify_mx_host("mx.notgoogle.com") == ("other", "other")

    def test_partial_yahoodns_does_not_match(self) -> None:
        """'fakeyahoodns.net' must not match the Yahoo pattern."""
        assert _classify_mx_host("mx.fakeyahoodns.net") == ("other", "other")


# ---------------------------------------------------------------------------
# enqueue_mx_lookup — mocked DB, no real network calls
# ---------------------------------------------------------------------------

_INSERT_ON_CONFLICT_PATTERN = re.compile(
    r"INSERT\s+INTO\s+domain_provider", re.IGNORECASE | re.DOTALL
)
_ON_CONFLICT_PATTERN = re.compile(r"ON\s+CONFLICT", re.IGNORECASE)


class TestEnqueueMxLookup:
    """Tests for enqueue_mx_lookup with a fake DB session.

    The goal is to assert that:
    1. An INSERT … ON CONFLICT statement is issued (idempotency).
    2. No real DB connection or DNS call is made.
    """

    @pytest.fixture()
    def fake_session(self) -> AsyncMock:
        """Return a minimal async context-manager mock for async_session_factory."""
        session = AsyncMock()
        session.execute = AsyncMock(return_value=None)
        session.commit = AsyncMock(return_value=None)
        # Support `async with async_session_factory() as session:`
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    async def test_insert_on_conflict_issued(self, fake_session: AsyncMock) -> None:
        """enqueue_mx_lookup must issue an INSERT … ON CONFLICT statement."""
        with (
            patch("asyncio.create_task", side_effect=_create_task_and_close),
            patch("app.core.db.async_session_factory", return_value=fake_session),
        ):
            await enqueue_mx_lookup("example.com", "otto")

        session = fake_session.__aenter__.return_value
        assert session.execute.called, "session.execute was never called"

        # Grab the SQL text from the first call
        call_args = session.execute.call_args_list[0]
        sql_obj = call_args[0][0]  # positional arg 0 of first call
        sql_text = str(sql_obj)
        assert _INSERT_ON_CONFLICT_PATTERN.search(
            sql_text
        ), f"Expected INSERT INTO domain_provider in SQL, got: {sql_text!r}"
        assert _ON_CONFLICT_PATTERN.search(
            sql_text
        ), f"Expected ON CONFLICT clause in SQL, got: {sql_text!r}"

    async def test_commit_called(self, fake_session: AsyncMock) -> None:
        """The session must be committed after the INSERT."""
        with (
            patch(
                "app.core.db.async_session_factory", return_value=fake_session
            ),
            patch("asyncio.create_task", side_effect=_create_task_and_close),
        ):
            await enqueue_mx_lookup("example.com", "otto")

        session = fake_session.__aenter__.return_value
        assert session.commit.called, "session.commit was never called"

    async def test_no_dns_on_enqueue(self, fake_session: AsyncMock) -> None:
        """enqueue_mx_lookup must NOT await DNS — that's the background task's job."""
        with (
            patch("app.core.db.async_session_factory", return_value=fake_session),
            patch("asyncio.create_task", side_effect=_create_task_and_close) as mock_create_task,
        ):
            await enqueue_mx_lookup("example.com", "otto")

        # The background task is scheduled via asyncio.create_task, not awaited here.
        mock_create_task.assert_called_once()

    async def test_idempotent_second_call(self, fake_session: AsyncMock) -> None:
        """Calling enqueue_mx_lookup twice with the same domain must issue the
        INSERT … ON CONFLICT each time (DB handles the idempotency via DO NOTHING).
        """
        with (
            patch("app.core.db.async_session_factory", return_value=fake_session),
            patch("asyncio.create_task", side_effect=_create_task_and_close),
        ):
            await enqueue_mx_lookup("example.com", "otto")
            await enqueue_mx_lookup("example.com", "otto")

        session = fake_session.__aenter__.return_value
        assert session.execute.call_count >= 2, (
            "Expected at least 2 execute calls for 2 enqueue calls"
        )
