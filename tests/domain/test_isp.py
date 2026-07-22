"""Tests for the pure domain→ISP classifier used by the reputation dashboard."""

from __future__ import annotations

import pytest

from app.domain.isp import (
    classify_isp_from_cloudwatch_label,
    classify_isp_from_domain,
    classify_isp_from_email,
)


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("gmail.com", "Gmail"),
        ("GMAIL.COM", "Gmail"),
        ("yahoo.com", "Yahoo"),
        ("yahoo.co.uk", "Yahoo"),
        ("ymail.com", "Yahoo"),
        ("hotmail.com", "Outlook"),
        ("outlook.com", "Outlook"),
        ("live.com", "Outlook"),
        ("msn.com", "Outlook"),
        ("icloud.com", "Apple"),
        ("me.com", "Apple"),
        ("mac.com", "Apple"),
        ("comcast.net", "Other"),
        ("aol.com", "Other"),
        ("", "Other"),
    ],
)
def test_classify_isp_from_domain(domain: str, expected: str) -> None:
    assert classify_isp_from_domain(domain) == expected


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("lead@gmail.com", "Gmail"),
        ("Lead@Yahoo.COM", "Yahoo"),
        ("lead@hotmail.com", "Outlook"),
        ("lead@icloud.com", "Apple"),
        ("lead@somethingelse.io", "Other"),
        ("not-an-email", "Other"),
    ],
)
def test_classify_isp_from_email(email: str, expected: str) -> None:
    assert classify_isp_from_email(email) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("GMAIL", "Gmail"),
        ("gmail", "Gmail"),
        ("YAHOO", "Yahoo"),
        ("HOTMAIL", "Outlook"),
        ("OUTLOOK", "Outlook"),
        ("LIVE", "Outlook"),
        ("MSN", "Outlook"),
        ("ICLOUD", "Apple"),
        ("APPLE", "Apple"),
        ("AOL", "Other"),
        ("COMCAST", "Other"),
        ("", "Other"),
    ],
)
def test_classify_isp_from_cloudwatch_label(label: str, expected: str) -> None:
    assert classify_isp_from_cloudwatch_label(label) == expected
