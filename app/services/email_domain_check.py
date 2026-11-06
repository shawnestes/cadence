"""Find email domains that cannot receive mail, before records are loaded.

A domain is undeliverable only on a definitive DNS answer: it does not exist,
it publishes a null MX (RFC 7505), it has neither MX nor address records
(RFC 5321 §5.1 implicit MX), or its name is malformed. Every other outcome —
timeout, SERVFAIL, refused, unreachable resolver — is inconclusive and the
domain is let through: a DNS outage must not empty an import.

No cache outlives a call. Callers resolve once per CLI run or per request; a
long-lived cache would keep rejecting a domain after it is registered.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable

import aiodns
import structlog

log = structlog.get_logger(__name__)

# Known-good domain used to confirm the resolver works before trusting any
# negative answer from it.
_PROBE_DOMAIN = "gmail.com"

ResolverFactory = Callable[[], aiodns.DNSResolver]


def _default_resolver() -> aiodns.DNSResolver:
    # Must be built inside the running loop: the constructor binds to it.
    return aiodns.DNSResolver(timeout=3, tries=2)


def _code(exc: aiodns.error.DNSError) -> int | None:
    return exc.args[0] if exc.args and isinstance(exc.args[0], int) else None


async def _has_address(resolver: aiodns.DNSResolver, domain: str, timeout: float) -> bool | None:
    """True if A or AAAA exists, False if definitively neither, None if unknown."""
    definitive_misses = 0
    for qtype in ("A", "AAAA"):
        try:
            result = await asyncio.wait_for(resolver.query_dns(domain, qtype), timeout)
        except aiodns.error.DNSError as exc:
            if _code(exc) in (aiodns.error.ARES_ENODATA, aiodns.error.ARES_ENOTFOUND):
                definitive_misses += 1
                continue
            return None
        except asyncio.TimeoutError:
            return None
        if result.answer:
            return True
        definitive_misses += 1
    return False if definitive_misses == 2 else None


async def _classify(
    resolver: aiodns.DNSResolver, domain: str, timeout: float
) -> tuple[str | None, bool]:
    """Return (undeliverable reason or None, inconclusive)."""
    try:
        result = await asyncio.wait_for(resolver.query_dns(domain, "MX"), timeout)
    except aiodns.error.DNSError as exc:
        code = _code(exc)
        if code == aiodns.error.ARES_ENOTFOUND:
            return "domain does not exist (NXDOMAIN)", False
        if code == aiodns.error.ARES_EBADNAME:
            return "malformed domain name", False
        if code != aiodns.error.ARES_ENODATA:
            return None, True
        has_address = await _has_address(resolver, domain, timeout)
        if has_address is None:
            return None, True
        return (None, False) if has_address else ("no MX or address record", False)
    except asyncio.TimeoutError:
        return None, True

    exchanges = [getattr(rec.data, "exchange", "") for rec in result.answer]
    if exchanges and all(not str(x).strip(".") for x in exchanges):
        return "domain accepts no mail (null MX)", False
    return None, False


async def find_undeliverable_domains(
    domains: Iterable[str],
    *,
    resolver_factory: ResolverFactory = _default_resolver,
    concurrency: int = 50,
    lookup_timeout: float = 8.0,
) -> dict[str, str]:
    """Map each definitively undeliverable domain to its reason.

    Domains absent from the result are deliverable or inconclusive. If the
    resolver cannot answer for a known-good domain, the check is skipped
    entirely and an empty mapping returned.
    """
    unique = sorted(set(domains))
    if not unique:
        return {}

    resolver = resolver_factory()
    try:
        try:
            await asyncio.wait_for(resolver.query_dns(_PROBE_DOMAIN, "MX"), lookup_timeout)
        except (aiodns.error.DNSError, asyncio.TimeoutError) as exc:
            log.warning("domain_check_unavailable", probe=_PROBE_DOMAIN, error=repr(exc))
            return {}

        semaphore = asyncio.Semaphore(concurrency)

        async def _one(domain: str) -> tuple[str, str | None, bool]:
            async with semaphore:
                reason, inconclusive = await _classify(resolver, domain, lookup_timeout)
                return domain, reason, inconclusive

        results = await asyncio.gather(*(_one(d) for d in unique))
    finally:
        await resolver.close()

    undeliverable = {d: reason for d, reason, _ in results if reason is not None}
    inconclusive = sorted(d for d, _, flag in results if flag)
    if inconclusive:
        log.warning(
            "domain_check_inconclusive",
            count=len(inconclusive),
            domains=inconclusive[:20],
        )
    log.info(
        "domain_check_complete",
        checked=len(unique),
        undeliverable=len(undeliverable),
        inconclusive=len(inconclusive),
    )
    return undeliverable
