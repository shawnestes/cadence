from __future__ import annotations

import ipaddress
from typing import Sequence

CLASS_HUMAN: int = 0
CLASS_MPP: int = 1

_APPLE_UA_KEYWORDS = ("icloud", "mail", "macos", "ios")


def classify_open(
    ip: str | None,
    user_agent: str | None,
    apple_proxy_cidrs: Sequence[str],
) -> int:
    """Return CLASS_MPP (1) when the open looks like Apple Mail Privacy Protection.

    MPP is detected when:
    - The IP falls within a known Apple proxy CIDR, OR
    - The user-agent contains "Apple" and at least one of "iCloud", "Mail",
      "macOS", or "iOS" (all checks are case-insensitive).

    Returns CLASS_HUMAN (0) for None/unparseable IPs or non-Apple agents.
    """
    # CIDR check
    if ip is not None and apple_proxy_cidrs:
        try:
            addr = ipaddress.ip_address(ip)
            for cidr in apple_proxy_cidrs:
                if addr in ipaddress.ip_network(cidr, strict=False):
                    return CLASS_MPP
        except ValueError:
            pass  # unparseable IP — fall through to UA check

    # User-agent check
    if user_agent is not None:
        ua_lower = user_agent.lower()
        if "apple" in ua_lower and any(kw in ua_lower for kw in _APPLE_UA_KEYWORDS):
            return CLASS_MPP

    return CLASS_HUMAN
