"""MX-based domain classification worker.

Enqueues non-blocking MX resolution for unknown domains and writes the
result back to domain_provider.  Never blocks the ingest hot path.

Design notes
------------
- No module-level side effects: async_session_factory and get_settings()
  are imported inside functions so CI runs without env vars set.
- Uses aiodns for native async DNS resolution (D7 approved dependency).
- asyncio.get_running_loop() is always used inside coroutines; never
  asyncio.get_event_loop() which is deprecated in async contexts.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from app.domain.isp import (
    DOMAIN_SOURCE_MX_LOOKUP,
    MAILBOX_PROVIDER_CODES,
    REPUTATION_GROUP_CODES,
)

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Pure classifier — no I/O, no imports, independently unit-testable.
# ---------------------------------------------------------------------------


def _classify_mx_host(mx_host: str) -> tuple[str, str]:
    """Classify an MX hostname into (mailbox_provider, reputation_group).

    Patterns (spec §3):
    - *.google.com or *.googlemail.com  → ('corporate', 'gmail')
    - *.protection.outlook.com or
      *.olc.protection.outlook.com      → ('corporate', 'microsoft')
    - *.yahoodns.net                    → ('other', 'yahoo')
    - anything else                     → ('other', 'other')

    Args:
        mx_host: The MX hostname. Normalized (lowercased, trailing dot
                 stripped) inside this function; callers need not pre-normalize.

    Returns:
        A ``(mailbox_provider, reputation_group)`` tuple of lowercase strings.
    """
    h = mx_host.lower().rstrip(".")

    # Google Workspace / Gmail infrastructure
    if h.endswith(".google.com") or h.endswith(".googlemail.com"):
        return ("corporate", "gmail")

    # Microsoft 365 / Exchange Online Protection
    if h.endswith(".protection.outlook.com"):
        return ("corporate", "microsoft")

    # Yahoo MX infrastructure
    if h.endswith(".yahoodns.net"):
        return ("other", "yahoo")

    return ("other", "other")


# ---------------------------------------------------------------------------
# Public entry point — safe to call on the hot ingest path.
# ---------------------------------------------------------------------------


async def enqueue_mx_lookup(domain: str, tenant: str) -> None:
    """Insert a domain_provider placeholder and schedule MX resolution.

    The DNS resolution happens in a background task; this function returns
    as soon as the INSERT completes.  Callers do not await DNS resolution.

    Args:
        domain: Email domain to resolve, e.g. ``"example.com"``.
        tenant: Tenant slug for the domain_provider row.
    """
    from sqlalchemy import text

    from app.core.db import async_session_factory

    mp_other = MAILBOX_PROVIDER_CODES["other"]
    rg_other = REPUTATION_GROUP_CODES["other"]

    async with async_session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO domain_provider
                    (domain, tenant, mailbox_provider, reputation_group, source)
                VALUES (:domain, :tenant, :mp, :rg, :source)
                ON CONFLICT (tenant, domain) DO NOTHING
                """
            ),
            {
                "domain": domain,
                "tenant": tenant,
                "mp": mp_other,
                "rg": rg_other,
                "source": DOMAIN_SOURCE_MX_LOOKUP,
            },
        )
        await session.commit()

    asyncio.create_task(_run_mx_lookup(domain, tenant))


# ---------------------------------------------------------------------------
# Background resolver — runs after enqueue_mx_lookup returns.
# ---------------------------------------------------------------------------


async def _run_mx_lookup(domain: str, tenant: str) -> None:  # noqa: ARG001
    """Resolve MX for *domain*, classify, and update domain_provider.

    Never raises — all exceptions are logged and swallowed so a failed
    lookup cannot crash the event loop.

    Args:
        domain: Email domain to resolve.
        tenant: Tenant slug (unused in UPDATE but kept for logging context).
    """
    import aiodns

    from sqlalchemy import text

    from app.core.db import async_session_factory

    t0 = time.monotonic()
    bound_log = log.bind(domain=domain, tenant=tenant)

    try:
        resolver = aiodns.DNSResolver()
        try:
            records = await resolver.query(domain, "MX")
        except aiodns.error.DNSError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            bound_log.warning(
                "mx_lookup_dns_error",
                error=str(exc),
                elapsed_ms=elapsed_ms,
            )
            return

        if not records:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            bound_log.info("mx_lookup_no_records", elapsed_ms=elapsed_ms)
            return

        # Lowest-priority value = most preferred MX (RFC 974).
        best = min(records, key=lambda r: r.priority)
        mx_host = best.host.lower().rstrip(".")

        mailbox_provider, reputation_group = _classify_mx_host(mx_host)

        mp_code = MAILBOX_PROVIDER_CODES.get(mailbox_provider, MAILBOX_PROVIDER_CODES["other"])
        rg_code = REPUTATION_GROUP_CODES.get(reputation_group, REPUTATION_GROUP_CODES["other"])

        async with async_session_factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE domain_provider
                       SET mx_host          = :mx_host,
                           mailbox_provider = :mp,
                           reputation_group = :rg,
                           checked_at       = now()
                     WHERE tenant = :tenant AND domain = :domain
                    """
                ),
                {
                    "mx_host": mx_host,
                    "mp": mp_code,
                    "rg": rg_code,
                    "tenant": tenant,
                    "domain": domain,
                },
            )
            await session.commit()

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        bound_log.info(
            "mx_lookup_complete",
            mx_host=mx_host,
            mailbox_provider=mailbox_provider,
            reputation_group=reputation_group,
            elapsed_ms=elapsed_ms,
        )

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        bound_log.exception(
            "mx_lookup_unexpected_error",
            error=str(exc),
            elapsed_ms=elapsed_ms,
        )
