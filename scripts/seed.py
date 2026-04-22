"""Populate the Cadence database with realistic demo data.

Usage:
    uv run python -m scripts.seed

Idempotent: removes any prior seed rows before re-inserting. Safe to re-run.

Seed rows are identifiable by:
- Recipients: email ends in ``@cadenceseed.io``
- Workflows / campaigns: name prefixed ``seed-``

Runs directly against whatever ``CADENCE_DATABASE_URL`` points at. Populates
sends + events spanning the last 14 days so the dashboard topline, timeseries,
campaigns-volume card, and recent-alerts table all have data to render.

Does NOT send any email. The ``esp`` column on sends is set to ``"noop"``.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.db import async_session_factory
from app.models import (
    Alert,
    Campaign,
    Event,
    Journey,
    Recipient,
    Send,
    Suppression,
    Template,
    Workflow,
    uuid7,
)

TENANT = "otto"
SEED_EMAIL_DOMAIN = "cadenceseed.io"
SEED_NAME_PREFIX = "seed-"
NOW = datetime.now(timezone.utc)
WINDOW_DAYS = 14

# Deterministic but varied.
random.seed(42)


async def clear_existing(s) -> None:
    """Delete any previously seeded rows. Safe to re-run."""
    await s.execute(text("""
        DELETE FROM events
        WHERE recipient_id IN (
            SELECT id FROM recipients WHERE email LIKE :like
        )
    """), {"like": f"%@{SEED_EMAIL_DOMAIN}"})
    await s.execute(text("""
        DELETE FROM sends
        WHERE recipient_id IN (
            SELECT id FROM recipients WHERE email LIKE :like
        )
    """), {"like": f"%@{SEED_EMAIL_DOMAIN}"})
    await s.execute(text("""
        DELETE FROM journeys
        WHERE recipient_id IN (
            SELECT id FROM recipients WHERE email LIKE :like
        )
    """), {"like": f"%@{SEED_EMAIL_DOMAIN}"})
    await s.execute(text("""
        DELETE FROM journeys
        WHERE workflow_id IN (
            SELECT id FROM workflows WHERE name LIKE :like
        )
    """), {"like": f"{SEED_NAME_PREFIX}%"})
    await s.execute(text("""
        DELETE FROM templates
        WHERE campaign_id IN (
            SELECT id FROM campaigns WHERE name LIKE :like
        )
    """), {"like": f"{SEED_NAME_PREFIX}%"})
    await s.execute(text("DELETE FROM campaigns WHERE name LIKE :like"),
                    {"like": f"{SEED_NAME_PREFIX}%"})
    await s.execute(text("DELETE FROM workflows WHERE name LIKE :like"),
                    {"like": f"{SEED_NAME_PREFIX}%"})
    await s.execute(text("DELETE FROM suppressions WHERE email LIKE :like"),
                    {"like": f"%@{SEED_EMAIL_DOMAIN}"})
    await s.execute(text("DELETE FROM recipients WHERE email LIKE :like"),
                    {"like": f"%@{SEED_EMAIL_DOMAIN}"})
    # Alerts don't have a direct "seed" flag — clear demo-flavored ones only.
    await s.execute(text("""
        DELETE FROM alerts
        WHERE tenant = :tenant
          AND notes IS NOT NULL
          AND notes LIKE 'seed:%'
    """), {"tenant": TENANT})


def make_workflow() -> Workflow:
    definition = {
        "entry": "entry",
        "nodes": {
            "entry": {"type": "entry", "next": "send_welcome"},
            "send_welcome": {
                "type": "send",
                "campaign_ref": "welcome",
                "next": "wait_1d",
            },
            "wait_1d": {"type": "wait", "duration": "PT24H", "next": "send_followup"},
            "send_followup": {
                "type": "send",
                "campaign_ref": "followup",
                "next": "exit",
            },
            "exit": {"type": "exit", "reason": "completed"},
        },
    }
    return Workflow(
        id=uuid7(),
        tenant=TENANT,
        name=f"{SEED_NAME_PREFIX}auto-welcome",
        vertical="auto",
        status="active",
        definition=definition,
        entry_segment={"vertical": "auto"},
        version=1,
        created_by="seed",
    )


def make_campaigns(workflow: Workflow) -> list[Campaign]:
    return [
        Campaign(
            id=uuid7(),
            tenant=TENANT,
            workflow_id=workflow.id,
            node_id="send_welcome",
            name=f"{SEED_NAME_PREFIX}welcome",
            vertical="auto",
            status="active",
            cohort_filter={"vertical": "auto"},
            ai_brief="Warm welcome email introducing OTTO auto insurance.",
            mab_config={"algorithm": "thompson", "min_explore_sends": 200},
        ),
        Campaign(
            id=uuid7(),
            tenant=TENANT,
            workflow_id=workflow.id,
            node_id="send_followup",
            name=f"{SEED_NAME_PREFIX}followup-quote",
            vertical="auto",
            status="active",
            cohort_filter={"vertical": "auto"},
            ai_brief="Follow-up email encouraging the recipient to get a quote.",
            mab_config={"algorithm": "thompson", "min_explore_sends": 200},
        ),
        Campaign(
            id=uuid7(),
            tenant=TENANT,
            workflow_id=None,
            node_id=None,
            name=f"{SEED_NAME_PREFIX}reactivation",
            vertical="auto",
            status="draft",
            cohort_filter={"vertical": "auto", "last_engaged_days_gte": 60},
            ai_brief="Reactivate dormant leads with a limited-time quote offer.",
            mab_config={"algorithm": "thompson", "min_explore_sends": 200},
        ),
    ]


def make_templates(campaign: Campaign) -> list[Template]:
    """Three MAB variants per campaign (different subject lines)."""
    tag = campaign.name.removeprefix(SEED_NAME_PREFIX)
    variants = [
        ("A", f"Welcome to OTTO — {tag} edition"),
        ("B", f"Your auto quote is ready — {tag}"),
        ("C", f"One click to save on auto coverage ({tag})"),
    ]
    statuses = ["approved", "approved", "generated"]
    out: list[Template] = []
    for (label, subject), status in zip(variants, statuses):
        out.append(Template(
            id=uuid7(),
            tenant=TENANT,
            campaign_id=campaign.id,
            cohort_key="vertical=auto",
            variant_label=label,
            subject=subject,
            preheader="Switch in 3 minutes. No calls, no pressure.",
            body_html=f"<p>Hi {{{{firstName}}}}, {subject}</p>",
            body_text=f"Hi {{{{firstName}}}}, {subject}",
            ai_meta={"prompt_version": "seed-v1", "tokens_in": 412, "tokens_out": 180},
            status=status,
        ))
    return out


FIRST_NAMES = ["Avery", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie",
               "Quinn", "Parker", "Reese", "Hayden", "Blake", "Drew", "Cameron",
               "Rowan", "Emerson", "Skyler", "Finley", "Logan", "Sage"]
LAST_NAMES = ["Chen", "Patel", "Gomez", "Nguyen", "Johnson", "Williams", "Brown",
              "Davis", "Garcia", "Miller", "Wilson", "Moore", "Anderson", "Thomas",
              "Martinez", "Lee", "Kim", "Park", "Murphy", "Clark"]


def make_recipients(n: int) -> list[Recipient]:
    out: list[Recipient] = []
    used: set[str] = set()
    for i in range(n):
        first = FIRST_NAMES[i % len(FIRST_NAMES)]
        last = LAST_NAMES[(i * 7) % len(LAST_NAMES)]
        email = f"{first.lower()}.{last.lower()}{i}@{SEED_EMAIL_DOMAIN}"
        if email in used:
            continue
        used.add(email)
        seen_at = NOW - timedelta(days=random.randint(0, WINDOW_DAYS),
                                   hours=random.randint(0, 23))
        out.append(Recipient(
            id=uuid7(),
            tenant=TENANT,
            email=email,
            external_id=f"OTTO-{i:06d}",
            vertical="auto",
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            consent_source="otto_lead_ingest",
            consent_at=seen_at,
            attributes={"firstName": first, "lastName": last,
                        "state": random.choice(["CA", "TX", "FL", "NY", "IL", "OH"])},
            status="active",
        ))
    return out


def make_sends_and_events(
    recipients: list[Recipient],
    campaigns: list[Campaign],
    templates_by_campaign: dict[uuid.UUID, list[Template]],
) -> tuple[list[Send], list[Event]]:
    """Generate 1–2 sends per recipient across the 14-day window.

    Distribution (rough):
      - 85% delivered
      - 8% bounced (split 70/30 hard/soft)
      - 0.5% complained
      - ~1.5% sent (in-flight)
      - ~5% queued

    For each delivered send, generate:
      - 25% open event, 5% click event, 0.8% unsubscribe event
    """
    sends: list[Send] = []
    events: list[Event] = []

    sendable = [c for c in campaigns if c.status == "active"]

    for r in recipients:
        n_sends = random.choice([1, 1, 2])  # mostly 1, some 2
        for k in range(n_sends):
            campaign = random.choice(sendable)
            template = random.choice(templates_by_campaign[campaign.id])

            days_ago = random.uniform(0, WINDOW_DAYS)
            queued_at = NOW - timedelta(days=days_ago)

            roll = random.random()
            if roll < 0.85:
                status = "delivered"
                failure_reason = None
            elif roll < 0.93:
                status = "bounced"
                failure_reason = "hard_bounce" if random.random() < 0.70 else "soft_bounce"
            elif roll < 0.935:
                status = "complained"
                failure_reason = None
            elif roll < 0.95:
                status = "sent"
                failure_reason = None
            else:
                status = "queued"
                failure_reason = None

            sent_at = None
            delivered_at = None
            if status in ("sent", "delivered", "bounced", "complained"):
                sent_at = queued_at + timedelta(seconds=random.randint(1, 60))
            if status == "delivered":
                delivered_at = sent_at + timedelta(seconds=random.randint(1, 120))

            send = Send(
                id=uuid7(),
                tenant=TENANT,
                journey_id=None,
                campaign_id=campaign.id,
                template_id=template.id,
                recipient_id=r.id,
                cohort_key=template.cohort_key,
                variant_label=template.variant_label,
                esp="noop",
                esp_message_id=f"seed-msg-{uuid.uuid4().hex[:12]}",
                rendered_subject=template.subject,
                status=status,
                queued_at=queued_at,
                sent_at=sent_at,
                delivered_at=delivered_at,
                failure_reason=failure_reason,
            )
            sends.append(send)

            if status == "bounced":
                events.append(Event(
                    id=uuid7(),
                    tenant=TENANT,
                    send_id=send.id,
                    recipient_id=r.id,
                    campaign_id=campaign.id,
                    variant_label=template.variant_label,
                    cohort_key=template.cohort_key,
                    event_type="bounce",
                    event_subtype="hard" if failure_reason == "hard_bounce" else "soft",
                    url=None,
                    user_agent=None,
                    ip_address=None,
                    raw_payload=None,
                    occurred_at=sent_at + timedelta(seconds=random.randint(1, 300)),
                ))
            if status == "complained":
                events.append(Event(
                    id=uuid7(),
                    tenant=TENANT,
                    send_id=send.id,
                    recipient_id=r.id,
                    campaign_id=campaign.id,
                    variant_label=template.variant_label,
                    cohort_key=template.cohort_key,
                    event_type="complaint",
                    event_subtype=None,
                    url=None,
                    user_agent=None,
                    ip_address=None,
                    raw_payload=None,
                    occurred_at=sent_at + timedelta(hours=random.randint(1, 12)),
                ))

            if status == "delivered":
                # Opens
                if random.random() < 0.25:
                    events.append(Event(
                        id=uuid7(),
                        tenant=TENANT,
                        send_id=send.id,
                        recipient_id=r.id,
                        campaign_id=campaign.id,
                        variant_label=template.variant_label,
                        cohort_key=template.cohort_key,
                        event_type="open",
                        event_subtype=None,
                        url=None,
                        user_agent="Mozilla/5.0 (seed)",
                        ip_address=None,
                        raw_payload=None,
                        occurred_at=delivered_at + timedelta(minutes=random.randint(5, 240)),
                    ))
                # Clicks
                if random.random() < 0.05:
                    events.append(Event(
                        id=uuid7(),
                        tenant=TENANT,
                        send_id=send.id,
                        recipient_id=r.id,
                        campaign_id=campaign.id,
                        variant_label=template.variant_label,
                        cohort_key=template.cohort_key,
                        event_type="click",
                        event_subtype=None,
                        url="https://otto.example.com/quote",
                        user_agent="Mozilla/5.0 (seed)",
                        ip_address=None,
                        raw_payload=None,
                        occurred_at=delivered_at + timedelta(minutes=random.randint(10, 360)),
                    ))
                # Unsubscribes
                if random.random() < 0.008:
                    events.append(Event(
                        id=uuid7(),
                        tenant=TENANT,
                        send_id=send.id,
                        recipient_id=r.id,
                        campaign_id=campaign.id,
                        variant_label=template.variant_label,
                        cohort_key=template.cohort_key,
                        event_type="unsubscribe",
                        event_subtype=None,
                        url=None,
                        user_agent="Mozilla/5.0 (seed)",
                        ip_address=None,
                        raw_payload=None,
                        occurred_at=delivered_at + timedelta(hours=random.randint(1, 48)),
                    ))
    return sends, events


def make_journeys(recipients: list[Recipient], workflow: Workflow) -> list[Journey]:
    """Create a few journey rows so the Recipient detail page shows some history."""
    out: list[Journey] = []
    sampled = random.sample(recipients, k=min(20, len(recipients)))
    for r in sampled:
        entered = NOW - timedelta(days=random.randint(0, WINDOW_DAYS))
        status = random.choice(["completed", "completed", "completed", "active", "exited"])
        completed = entered + timedelta(days=1) if status != "active" else None
        current_node = "exit" if status != "active" else "wait_1d"
        out.append(Journey(
            id=uuid7(),
            tenant=TENANT,
            workflow_id=workflow.id,
            workflow_version=workflow.version,
            recipient_id=r.id,
            current_node_id=current_node,
            next_action_at=None if status != "active"
            else entered + timedelta(days=1),
            status=status,
            context={},
            entered_at=entered,
            completed_at=completed,
        ))
    return out


def make_suppressions(recipients: list[Recipient]) -> list[Suppression]:
    out: list[Suppression] = []
    sample = random.sample(recipients, k=min(8, len(recipients)))
    reasons = ["unsubscribe", "unsubscribe", "hard_bounce", "complaint",
               "manual", "otto_push", "unsubscribe", "hard_bounce"]
    for r, reason in zip(sample, reasons):
        when = NOW - timedelta(days=random.randint(0, WINDOW_DAYS))
        out.append(Suppression(
            id=uuid7(),
            tenant=TENANT,
            email=r.email,
            reason=reason,
            source=("webhook" if reason in ("hard_bounce", "complaint")
                    else "user" if reason == "unsubscribe"
                    else "otto" if reason == "otto_push"
                    else "admin"),
            campaign_id=None,
            notes="seed: demo suppression",
            suppressed_at=when,
        ))
    return out


def make_alerts() -> list[Alert]:
    two_hours_ago = NOW - timedelta(hours=2)
    yesterday = NOW - timedelta(days=1)
    day_before = NOW - timedelta(days=2)
    return [
        # Active critical bounce alert
        Alert(
            id=uuid7(),
            tenant=TENANT,
            metric="bounce_rate",
            severity="critical",
            threshold=0.05,
            value=0.072,
            status="active",
            pending_since=two_hours_ago - timedelta(minutes=45),
            activated_at=two_hours_ago - timedelta(minutes=15),
            resolved_at=None,
            notes="seed: demo active alert (bounce rate above SES critical)",
        ),
        # Resolved warn complaint alert from yesterday
        Alert(
            id=uuid7(),
            tenant=TENANT,
            metric="complaint_rate",
            severity="warn",
            threshold=0.0007,
            value=0.0009,
            status="resolved",
            pending_since=day_before,
            activated_at=day_before + timedelta(minutes=30),
            resolved_at=yesterday,
            notes="seed: demo resolved alert (complaint rate recovered)",
        ),
    ]


async def main() -> None:
    print(f"Seeding Cadence demo data against tenant={TENANT} at {NOW.isoformat()}")
    async with async_session_factory() as s:
        await clear_existing(s)

        workflow = make_workflow()
        s.add(workflow)
        await s.flush()

        campaigns = make_campaigns(workflow)
        for c in campaigns:
            s.add(c)
        await s.flush()

        templates_by_campaign: dict[uuid.UUID, list[Template]] = {}
        for c in campaigns:
            ts = make_templates(c)
            templates_by_campaign[c.id] = ts
            for t in ts:
                s.add(t)
        await s.flush()

        recipients = make_recipients(60)
        for r in recipients:
            s.add(r)
        await s.flush()

        journeys = make_journeys(recipients, workflow)
        for j in journeys:
            s.add(j)
        await s.flush()

        sends, events = make_sends_and_events(recipients, campaigns, templates_by_campaign)
        for snd in sends:
            s.add(snd)
        await s.flush()
        for e in events:
            s.add(e)
        await s.flush()

        for sup in make_suppressions(recipients):
            s.add(sup)

        for a in make_alerts():
            s.add(a)

        await s.commit()

    print(f"  workflows:    1")
    print(f"  campaigns:    {len(campaigns)}")
    print(f"  templates:    {sum(len(v) for v in templates_by_campaign.values())}")
    print(f"  recipients:   {len(recipients)}")
    print(f"  journeys:     {len(journeys)}")
    print(f"  sends:        {len(sends)}")
    print(f"  events:       {len(events)}")
    print(f"  suppressions: 8")
    print(f"  alerts:       2 (1 active critical, 1 resolved warn)")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
