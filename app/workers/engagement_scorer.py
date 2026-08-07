"""engagement_scorer — RETIRED.

This worker previously classified recipients into engagement tiers by reading
from the legacy `events` table and writing to `recipients.engagement_tier`.

That responsibility has been transferred to the `profile_refresh` worker, which
reads from `contact_event`, writes to `contact_profile.engagement_tier`, and is
the segment-layer source of truth consumed by `segment_member_current`.

This module is kept as a no-op stub so that any scheduler or caller referencing
`run_once` does not crash. It must not be re-enabled without updating callers to
use `profile_refresh.run_once` instead.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


async def run_once() -> dict:
    """No-op stub. This worker is retired; profile_refresh owns tier computation."""
    log.warning(
        "engagement_scorer.deprecated",
        message=(
            "engagement_scorer is retired and will not score recipients. "
            "Engagement tier computation is now owned by profile_refresh, "
            "which reads from contact_event and writes to contact_profile."
        ),
    )
    return {"scored": 0, "changed": 0, "dormant_transitions": 0}
