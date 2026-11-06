"""find_undeliverable_domains over a fake resolver shaped like aiodns 4."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiodns
import pytest

from app.services.email_domain_check import find_undeliverable_domains

_NXDOMAIN = aiodns.error.DNSError(aiodns.error.ARES_ENOTFOUND, "Domain name not found")
_NODATA = aiodns.error.DNSError(aiodns.error.ARES_ENODATA, "no data")
_BADNAME = aiodns.error.DNSError(aiodns.error.ARES_EBADNAME, "Misformatted domain name")
_SERVFAIL = aiodns.error.DNSError(aiodns.error.ARES_ESERVFAIL, "Server failure")


def _mx(*exchanges: str) -> SimpleNamespace:
    return SimpleNamespace(answer=[SimpleNamespace(data=SimpleNamespace(exchange=x)) for x in exchanges])


def _a() -> SimpleNamespace:
    return SimpleNamespace(answer=[SimpleNamespace(data=SimpleNamespace(addr="192.0.2.1"))])


_HANG = object()


class _FakeResolver:
    def __init__(self, answers: dict[tuple[str, str], object]) -> None:
        self.answers = answers
        self.closed = False

    async def query_dns(self, domain: str, qtype: str):
        answer = self.answers.get((domain, qtype), _NXDOMAIN)
        if answer is _HANG:
            await asyncio.sleep(10)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def close(self) -> None:
        self.closed = True


def _resolver(answers: dict[tuple[str, str], object]) -> _FakeResolver:
    return _FakeResolver({("gmail.com", "MX"): _mx("gmail-smtp-in.l.google.com"), **answers})


async def test_classifies_each_definitive_dns_answer() -> None:
    fake = _resolver(
        {
            ("live.example", "MX"): _mx("mx.live.example"),
            ("nullmx.example", "MX"): _mx(""),
            ("arecord.example", "MX"): _NODATA,
            ("arecord.example", "A"): _a(),
            ("nothing.example", "MX"): _NODATA,
            ("nothing.example", "A"): _NODATA,
            ("nothing.example", "AAAA"): _NODATA,
            ("bad..example", "MX"): _BADNAME,
        }
    )
    result = await find_undeliverable_domains(
        ["live.example", "gone.example", "nullmx.example", "arecord.example",
         "nothing.example", "bad..example"],
        resolver_factory=lambda: fake,
    )
    assert result == {
        "gone.example": "domain does not exist (NXDOMAIN)",
        "nullmx.example": "domain accepts no mail (null MX)",
        "nothing.example": "no MX or address record",
        "bad..example": "malformed domain name",
    }
    assert fake.closed


async def test_inconclusive_answers_let_the_domain_through() -> None:
    fake = _resolver(
        {
            ("servfail.example", "MX"): _SERVFAIL,
            ("slow.example", "MX"): _HANG,
            ("aservfail.example", "MX"): _NODATA,
            ("aservfail.example", "A"): _SERVFAIL,
        }
    )
    result = await find_undeliverable_domains(
        ["servfail.example", "slow.example", "aservfail.example"],
        resolver_factory=lambda: fake,
        lookup_timeout=0.05,
    )
    assert result == {}


async def test_empty_mx_answer_falls_back_to_address_records() -> None:
    fake = _resolver(
        {
            ("empty-dead.example", "MX"): _mx(),
            ("empty-dead.example", "A"): _NODATA,
            ("empty-dead.example", "AAAA"): _NODATA,
            ("empty-live.example", "MX"): _mx(),
            ("empty-live.example", "A"): _a(),
        }
    )
    result = await find_undeliverable_domains(
        ["empty-dead.example", "empty-live.example"], resolver_factory=lambda: fake
    )
    assert result == {"empty-dead.example": "no MX or address record"}


async def test_timeout_bounds_the_whole_lookup_sequence() -> None:
    # Each query alone finishes inside the timeout; MX + A + AAAA together don't.
    class _Slow(_FakeResolver):
        async def query_dns(self, domain: str, qtype: str):
            if domain != "gmail.com":
                await asyncio.sleep(0.15)
            return await super().query_dns(domain, qtype)

    fake = _Slow(
        {
            ("gmail.com", "MX"): _mx("gmail-smtp-in.l.google.com"),
            ("slowchain.example", "MX"): _NODATA,
            ("slowchain.example", "A"): _NODATA,
            ("slowchain.example", "AAAA"): _NODATA,
        }
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await find_undeliverable_domains(
        ["slowchain.example"], resolver_factory=lambda: fake, lookup_timeout=0.25
    )
    assert result == {}
    assert loop.time() - started < 0.4


@pytest.mark.parametrize("probe_failure", [_SERVFAIL, _HANG])
async def test_skips_the_check_when_the_resolver_cannot_answer(probe_failure: object) -> None:
    # The resolver says every domain is NXDOMAIN, but it can't resolve gmail.com
    # either, so none of its negatives can be trusted.
    fake = _FakeResolver({("gmail.com", "MX"): probe_failure})
    result = await find_undeliverable_domains(
        ["gone.example"], resolver_factory=lambda: fake, lookup_timeout=0.05
    )
    assert result == {}
    assert fake.closed
