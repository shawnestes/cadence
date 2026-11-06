from __future__ import annotations

import pytest

from app.domain.email_typos import probable_typo


@pytest.mark.parametrize(
    ("domain", "intended"),
    [
        ("gmai.com", "gmail.com"),
        ("gmial.com", "gmail.com"),
        ("gamil.com", "gmail.com"),
        ("gmaill.com", "gmail.com"),
        ("gmail.con", "gmail.com"),
        ("gmail.co", "gmail.com"),
        ("yaho.com", "yahoo.com"),
        ("yahooo.com", "yahoo.com"),
        ("hotmial.com", "hotmail.com"),
        ("hotmal.com", "hotmail.com"),
        ("outookl.com", "outlook.com"),
        ("outlok.com", "outlook.com"),
        ("iclod.com", "icloud.com"),
        ("comcats.net", "comcast.net"),
        ("sbcgloabl.net", "sbcglobal.net"),
        ("aol.co", "aol.com"),
        ("att.nt", "att.net"),
    ],
)
def test_flags_near_misses_of_major_providers(domain: str, intended: str) -> None:
    assert probable_typo(domain) == intended


@pytest.mark.parametrize(
    "domain",
    [
        # the providers themselves
        "gmail.com", "yahoo.com", "outlook.com", "aol.com", "ymail.com", "me.com",
        # real domains one or two edits from a provider
        "mail.com", "email.com", "cloud.com", "gmx.com", "aim.com", "mac.com",
        "love.com", "fox.net", "cox.com", "at.net",
        # country-code variants of providers are real, not misspelled TLDs
        "hotmail.ca", "hotmail.co.uk", "protonmail.ch", "outlook.cz", "yahoo.co.uk",
        "comcast.com",
        # ordinary ISP / corporate domains seen in OTTO files
        "flash.net", "wi.rr.com", "carolina.rr.com", "mindspring.com", "fastmail.fm",
        "windstream.net", "barkmanrealtygroup.com",
    ],
)
def test_leaves_real_domains_alone(domain: str) -> None:
    assert probable_typo(domain) is None
