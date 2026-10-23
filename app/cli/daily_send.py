"""Queue a daily batch of sends for one campaign.

Cron entry point. This module only *queues* rows into `sends`; the long-running
send worker performs the actual dispatch and owns every compliance gate
(suppression at send time, engagement-gap throttle, weekly/monthly frequency
caps, quiet hours, account quota, rate limit). Keeping the two separate means a
cron misfire can never bypass a gate -- it can only enqueue work the worker is
still free to refuse.

Selection rules:

* tenant + campaign's vertical must match the recipient
* recipient must be `active` (never suppressed)
* recipient must have no prior `sends` row -- this job never re-mails anyone
* freshest consent first: aged remarketing records draw more complaints, so a
  limited daily batch should spend itself on the newest leads

Safety properties:

* One statement. `INSERT ... SELECT ... WHERE NOT EXISTS` is evaluated
  atomically, so two concurrent runs cannot double-queue the same recipient.
* `--limit` is required to be positive and is the hard ceiling on rows created.
* `--dry-run` reports the exact set that would be queued and writes nothing.

Warm-up ramp
------------

`--ramp` replaces `--limit` with a schedule indexed by how many calendar days
this campaign has been sending, derived from `min(sent_at)` on its own sends.
No state file, nothing to keep in sync -- the campaign's history *is* the
cursor, so a skipped day does not shift the ramp and a re-run on the same day
reuses the same rung.

A new sending domain has no reputation. Mailbox providers read a jump from a
near-zero baseline to full volume as spam behaviour, so the published guidance
is to grow gradually and never roughly double day over day. The rungs below
reach 50/day over two weeks and hold.

Usage:

    uv run python -m app.cli.daily_send --campaign OttoDaily --ramp
    uv run python -m app.cli.daily_send --campaign OttoDaily --limit 50
    uv run python -m app.cli.daily_send --campaign OttoDaily --ramp --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog
from sqlalchemy import text

from app.core.db import async_session_factory
from app.core.logging import configure_logging
from app.core.config import get_settings

log = structlog.get_logger(__name__)

# (day_offset, daily_cap) -- day 0 is the campaign's first sending day. The cap
# in force is the last rung whose offset has been reached, so the final entry
# is the steady-state ceiling and the ramp never needs an end date.
# Growth stays at or under ~50% per step: a 10 -> 50 jump is the exact spike
# that gets a cold domain filtered.
RAMP: tuple[tuple[int, int], ...] = (
    (0, 10),
    (2, 15),
    (4, 20),
    (6, 25),
    (8, 30),
    (10, 40),
    (12, 50),
)


def ramp_limit(days_sending: int) -> int:
    """Daily cap for a campaign that has been sending for `days_sending` days."""
    cap = RAMP[0][1]
    for offset, value in RAMP:
        if days_sending >= offset:
            cap = value
        else:
            break
    return cap

# Shared predicate for "eligible, never-mailed recipient on this campaign's
# vertical", newest consent first. Used by both the dry run and the insert so
# the two can never disagree about who is in scope.
_ELIGIBLE = """
    SELECT r.id
      FROM recipients r
      JOIN contact_profile cp
        ON cp.contact_id = r.id AND cp.tenant = r.tenant
     WHERE r.tenant = :tenant
       AND r.status = 'active'
       AND r.vertical = :vertical
       AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.recipient_id = r.id)
       AND NOT EXISTS (
             SELECT 1 FROM suppressions p
              WHERE p.tenant = r.tenant AND p.email = r.email
           )
     ORDER BY cp.consent_at DESC
     LIMIT :limit
"""

_RESOLVE_CAMPAIGN = text("""
    SELECT c.id, c.tenant, c.vertical, c.status, c.sending_identity_id,
           t.id AS template_id, t.cohort_key, t.variant_label, t.subject
      FROM campaigns c
      JOIN templates t ON t.campaign_id = c.id AND t.status = 'approved'
     WHERE c.name = :campaign
""")

# Days this campaign has been sending: 0 on the first day, counted from the
# oldest dispatched send. NULL (never sent) also yields 0 via COALESCE.
_DAYS_SENDING = text("""
    SELECT COALESCE(
             (now()::date - MIN(sent_at)::date),
             0
           ) AS days
      FROM sends
     WHERE campaign_id = :campaign_id
       AND sent_at IS NOT NULL
""")

_PREVIEW = text(f"""
    SELECT r.email, r.attributes->>'state' AS state, cp.consent_at
      FROM recipients r
      JOIN contact_profile cp ON cp.contact_id = r.id AND cp.tenant = r.tenant
     WHERE r.id IN ({_ELIGIBLE})
     ORDER BY cp.consent_at DESC
""")

_INSERT = text(f"""
    INSERT INTO sends (id, tenant, campaign_id, template_id, recipient_id,
                       cohort_key, variant_label, esp, sending_identity_id,
                       rendered_subject, status, queued_at)
    SELECT gen_random_uuid(), :tenant, :campaign_id, :template_id, e.id,
           :cohort_key, :variant_label, :esp, :sending_identity_id,
           :subject, 'queued', now()
      FROM ({_ELIGIBLE}) AS e
    RETURNING recipient_id
""")


async def run(campaign: str, limit: int | None, ramp: bool, dry_run: bool) -> int:
    settings = get_settings()

    async with async_session_factory() as session:
        rows = (await session.execute(
            _RESOLVE_CAMPAIGN, {"campaign": campaign}
        )).mappings().all()

        if not rows:
            log.error("daily_send.no_approved_template", campaign=campaign)
            print(
                f"error: campaign {campaign!r} not found, or it has no approved "
                f"template. Nothing queued.",
                file=sys.stderr,
            )
            return 2

        if len(rows) > 1:
            # Several approved variants means MAB should pick per recipient,
            # which this bulk INSERT cannot express. Refuse rather than silently
            # pinning every recipient to one arm.
            labels = ", ".join(sorted(r["variant_label"] for r in rows))
            log.error(
                "daily_send.multiple_approved_templates",
                campaign=campaign,
                variants=labels,
            )
            print(
                f"error: campaign {campaign!r} has {len(rows)} approved templates "
                f"({labels}). This job queues a single variant; use the "
                f"campaign send endpoint for MAB. Nothing queued.",
                file=sys.stderr,
            )
            return 2

        c = rows[0]
        if c["status"] not in ("ready", "active"):
            log.error(
                "daily_send.campaign_not_sendable",
                campaign=campaign,
                status=c["status"],
            )
            print(
                f"error: campaign {campaign!r} has status {c['status']!r}; "
                f"expected 'ready' or 'active'. Nothing queued.",
                file=sys.stderr,
            )
            return 2

        scope = {
            "tenant": c["tenant"],
            "vertical": c["vertical"],
            "limit": limit,
        }

        if ramp:
            days = (await session.execute(
                _DAYS_SENDING, {"campaign_id": c["id"]}
            )).scalar_one()
            limit = ramp_limit(int(days))
            scope["limit"] = limit
            log.info(
                "daily_send.ramp",
                campaign=campaign,
                days_sending=int(days),
                limit=limit,
            )
            print(f"ramp: day {int(days)} of sending -> limit {limit}")

        if dry_run:
            preview = (await session.execute(_PREVIEW, scope)).mappings().all()
            print(f"dry run: would queue {len(preview)} send(s) for {campaign}")
            for p in preview:
                print("   %-40s %-3s consent=%s" % (
                    p["email"], p["state"] or "??", p["consent_at"].date()
                ))
            return 0

        esp = (
            c["sending_identity_id"] and "ses_smtp"
        ) or settings.esp_backend

        queued = (await session.execute(_INSERT, {
            **scope,
            "campaign_id": c["id"],
            "template_id": c["template_id"],
            "cohort_key": c["cohort_key"],
            "variant_label": c["variant_label"],
            "esp": esp,
            "sending_identity_id": c["sending_identity_id"],
            "subject": c["subject"],
        })).all()
        await session.commit()

        log.info(
            "daily_send.queued",
            campaign=campaign,
            requested=limit,
            queued=len(queued),
        )
        print(f"queued={len(queued)} requested={limit} campaign={campaign}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Queue a daily batch of sends for one campaign."
    )
    parser.add_argument("--campaign", required=True, help="campaign name")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--limit", type=int,
        help="maximum sends to queue this run (hard ceiling)",
    )
    group.add_argument(
        "--ramp", action="store_true",
        help="derive the cap from the warm-up schedule and this campaign's "
             "own sending history",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report the batch and write nothing",
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        print("error: --limit must be >= 1", file=sys.stderr)
        return 2

    configure_logging(get_settings().log_level)
    return asyncio.run(run(args.campaign, args.limit, args.ramp, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
