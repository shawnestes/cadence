from __future__ import annotations

from datetime import datetime
from typing import Sequence

CLASS_HUMAN: int = 0
CLASS_SCANNER: int = 2


def classify_click(
    user_agent: str | None,
    ip: str | None,
    clicked_at: datetime,
    delivered_at: datetime | None,
    other_links_same_second: int,
    has_origin_platform_confirm: bool,
    confirm_window_s: int,
    known_scanner_uas: Sequence[str],
    apple_proxy_cidrs: Sequence[str],
) -> int:
    """Classify a click event as human (0) or scanner (2).

    Synchronously evaluated rules:
    - Rule 2: ``other_links_same_second >= 1`` — multiple distinct links clicked
      within the same wall-clock second implies link-prefetch automation.
    - Rule 3: ``delivered_at`` is known and ``clicked_at - delivered_at < 2 s`` —
      a click arriving less than two seconds after delivery cannot be human.
    - Rule 4: any entry in ``known_scanner_uas`` is a case-insensitive substring
      of ``user_agent`` — explicit scanner user-agent match.

    Deferred rule (NOT evaluated here):
    - Rule 1: no origin-platform confirmation within ``confirm_window_s`` seconds.
      ``has_origin_platform_confirm`` and ``confirm_window_s`` are accepted for
      API stability but are ignored by this function; the deferred pass applies
      Rule 1 after the confirmation window elapses.

    ``ip`` and ``apple_proxy_cidrs`` are accepted for API stability (reserved for
    a future MPP-click rule) but are not evaluated in this version.

    Returns CLASS_SCANNER (2) if any of rules 2–4 fire; CLASS_HUMAN (0) otherwise.
    """
    # Rule 2: burst of links within same second
    if other_links_same_second >= 1:
        return CLASS_SCANNER

    # Rule 3: click arrived suspiciously fast after delivery
    if delivered_at is not None:
        delta = (clicked_at - delivered_at).total_seconds()
        if delta < 2.0:
            return CLASS_SCANNER

    # Rule 4: known scanner user-agent substring match
    if user_agent is not None:
        ua_lower = user_agent.lower()
        for scanner_ua in known_scanner_uas:
            if scanner_ua.lower() in ua_lower:
                return CLASS_SCANNER

    return CLASS_HUMAN
