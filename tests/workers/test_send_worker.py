"""Tests for the send worker (run_once)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest_asyncio
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import async_session_factory
from app.domain.engagement import TIER_COLD, TIER_DORMANT, TIER_ENGAGED
from app.domain.timezone import next_window_open as _real_next_window_open
from app.models.campaign import Campaign
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.suppression import Suppression
from app.models.template import Template
from app.workers import send_worker

# Sorts ahead of any realistically-timestamped queued row on the shared DB,
# so our seeded rows are the ones the phase-1 claim query picks up first.
_ANCIENT = datetime(2000, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def _open_all_hours(monkeypatch):
    """Default every send to "inside the quiet-hours window" so tests that
    aren't specifically exercising SPEC_ADDENDUM_002 quiet hours don't flake
    depending on the real wall-clock time in the recipient's local zone.
    Quiet-hours tests restore the real implementation via monkeypatch.
    """
    monkeypatch.setattr(send_worker, "next_window_open", lambda **kwargs: kwargs["now_utc"])
    yield


async def _read_send(send_id):
    async with async_session_factory() as s:
        return (await s.execute(select(Send).where(Send.id == send_id))).scalar_one_or_none()


async def _seed_recipient(
    db_session,
    email: str,
    attrs: dict | None = None,
    engagement_tier: str = "engaged",
):
    """Seed a recipient. Defaults to 'engaged' tier (24h gap) so pre-existing
    tests written before the engagement-gap gate landed keep their original
    intent without every caller having to specify a tier.
    """
    now = datetime.now(timezone.utc)
    r = Recipient(
        tenant="otto",
        email=email,
        external_id="otto_test",
        vertical="auto",
        first_seen_at=now,
        last_seen_at=now,
        consent_source="otto_form",
        consent_at=now,
        attributes=attrs or {"first_name": "Sarah"},
        engagement_tier=engagement_tier,
    )
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)
    return r


async def _seed_campaign_template(
    db_session,
    subject: str = "Hi {{first_name}}",
    body_html: str = "<p>Hi {{first_name}}</p>",
    body_text: str = "Hi {{first_name}}",
):
    c = Campaign(
        tenant="otto",
        name=f"test-campaign-{uuid.uuid4()}",
        vertical="auto",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)

    t = Template(
        tenant="otto",
        campaign_id=c.id,
        cohort_key=f"test-cohort-{uuid.uuid4()}",
        variant_label="A",
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        status="approved",
    )
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return c, t


async def _seed_queued_send(db_session, campaign, template, recipient, queued_at=None):
    s = Send(
        tenant="otto",
        campaign_id=campaign.id,
        template_id=template.id,
        recipient_id=recipient.id,
        cohort_key=template.cohort_key,
        variant_label=template.variant_label,
        esp="noop",
        rendered_subject=template.subject,
        status="queued",
        queued_at=queued_at or datetime.now(timezone.utc),
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


async def _seed_prior_sent_send(db_session, campaign, template, recipient, sent_at):
    """Seed a Send row that already went out, to backfill quota/throttle history."""
    now = datetime.now(timezone.utc)
    s = Send(
        tenant="otto",
        campaign_id=campaign.id,
        template_id=template.id,
        recipient_id=recipient.id,
        cohort_key=template.cohort_key,
        variant_label=template.variant_label,
        esp="noop",
        esp_message_id=f"noop-{uuid.uuid4()}",
        rendered_subject=template.subject,
        status="sent",
        queued_at=now,
        sent_at=sent_at,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


async def _count_sent_today(db_session) -> int:
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    result = await db_session.execute(
        select(func.count()).select_from(Send).where(Send.sent_at >= today_start)
    )
    return result.scalar_one()


def _settings_override(monkeypatch, **overrides):
    """Monkeypatch send_worker's cached settings with the given overrides applied."""
    base = get_settings()
    patched = base.model_copy(update=overrides)
    monkeypatch.setattr(send_worker, "get_settings", lambda: patched)
    return patched


async def test_run_once_sends_queued_row(db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    c, t = await _seed_campaign_template(db_session)
    s = await _seed_queued_send(db_session, c, t, r)

    await send_worker.run_once()

    refreshed = await _read_send(s.id)
    assert refreshed is not None
    assert refreshed.status == "sent"
    assert refreshed.sent_at is not None
    assert refreshed.esp_message_id is not None
    assert refreshed.esp_message_id.startswith("noop-")


async def test_run_once_suppressed_skip(db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    c, t = await _seed_campaign_template(db_session)
    s = await _seed_queued_send(db_session, c, t, r)

    db_session.add(
        Suppression(
            tenant="otto",
            email=email,
            reason="unsubscribe",
            source="one_click",
        )
    )
    await db_session.commit()

    await send_worker.run_once()

    refreshed = await _read_send(s.id)
    assert refreshed.status == "failed"
    assert refreshed.failure_reason is not None
    assert "suppressed" in refreshed.failure_reason


async def test_run_once_missing_merge_tag(db_session):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email, attrs={})
    c, t = await _seed_campaign_template(
        db_session,
        subject="Hi",
        body_html="<p>Hello {{nonexistent_var}}</p>",
        body_text="Hello {{nonexistent_var}}",
    )
    s = await _seed_queued_send(db_session, c, t, r)

    await send_worker.run_once()

    refreshed = await _read_send(s.id)
    assert refreshed.status == "failed"
    assert refreshed.failure_reason is not None
    assert "merge tag" in refreshed.failure_reason.lower()


# --- Per-day account quota ---------------------------------------------------


async def test_run_once_quota_exhausted_leaves_sends_queued(db_session, monkeypatch):
    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    c, t = await _seed_campaign_template(db_session)
    s = await _seed_queued_send(db_session, c, t, r, queued_at=_ANCIENT)

    sent_today = await _count_sent_today(db_session)
    # Quota already met (or exceeded) by whatever's been sent today, so this
    # cycle must dispatch nothing.
    _settings_override(monkeypatch, send_daily_quota=sent_today)

    result = await send_worker.run_once()

    assert result["sent"] == 0
    assert result["failed"] == 0

    refreshed = await _read_send(s.id)
    assert refreshed.status == "queued"
    assert refreshed.failure_reason is None


async def test_run_once_quota_decrements_within_cycle(db_session, monkeypatch):
    email_a = f"test-{uuid.uuid4()}@cadencetests.io"
    email_b = f"test-{uuid.uuid4()}@cadencetests.io"
    r_a = await _seed_recipient(db_session, email_a)
    r_b = await _seed_recipient(db_session, email_b)
    c, t = await _seed_campaign_template(db_session)
    s_a = await _seed_queued_send(db_session, c, t, r_a, queued_at=_ANCIENT)
    s_b = await _seed_queued_send(
        db_session, c, t, r_b, queued_at=_ANCIENT + timedelta(microseconds=1)
    )

    sent_today = await _count_sent_today(db_session)
    # Room for exactly one more send this cycle: the in-memory decrement
    # (not a fresh per-send COUNT query) must stop the second one.
    _settings_override(monkeypatch, send_daily_quota=sent_today + 1)

    result = await send_worker.run_once()

    assert result["sent"] == 1

    refreshed_a = await _read_send(s_a.id)
    refreshed_b = await _read_send(s_b.id)
    statuses = {refreshed_a.status, refreshed_b.status}
    assert statuses == {"sent", "queued"}
    # Whichever one didn't send stayed queued, not failed.
    for row in (refreshed_a, refreshed_b):
        assert row.failure_reason is None


# --- Per-recipient throttle (max 1 send / 24h) -------------------------------


async def test_run_once_recipient_throttled_within_24h(db_session, monkeypatch):
    _settings_override(monkeypatch, send_daily_quota=10_000_000)

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    c, t = await _seed_campaign_template(db_session)
    now = datetime.now(timezone.utc)
    await _seed_prior_sent_send(db_session, c, t, r, sent_at=now - timedelta(hours=1))
    s = await _seed_queued_send(db_session, c, t, r, queued_at=_ANCIENT)

    result = await send_worker.run_once()

    assert result["throttled"] == 1
    assert result["sent"] == 0

    refreshed = await _read_send(s.id)
    assert refreshed.status == "queued"
    assert refreshed.failure_reason is None
    # Rescheduled forward, not left claimable immediately (avoids a hot spin
    # loop re-claiming the same throttled recipient every cycle).
    assert refreshed.queued_at > now


async def test_run_once_recipient_sent_after_24h(db_session, monkeypatch):
    _settings_override(monkeypatch, send_daily_quota=10_000_000)

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)
    c, t = await _seed_campaign_template(db_session)
    now = datetime.now(timezone.utc)
    await _seed_prior_sent_send(db_session, c, t, r, sent_at=now - timedelta(hours=25))
    s = await _seed_queued_send(db_session, c, t, r, queued_at=_ANCIENT)

    result = await send_worker.run_once()

    assert result["sent"] == 1

    refreshed = await _read_send(s.id)
    assert refreshed.status == "sent"
    assert refreshed.sent_at is not None


# --- Engagement-tier gap (SPEC_ADDENDUM_002 §2.2) ----------------------------


async def test_run_once_engaged_tier_sent_after_25h(db_session, monkeypatch):
    """Engaged tier's gap is 24h (same as the old flat throttle): a send from
    25h ago is old enough to clear the gate."""
    _settings_override(monkeypatch, send_daily_quota=10_000_000)

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email, engagement_tier=TIER_ENGAGED)
    c, t = await _seed_campaign_template(db_session)
    now = datetime.now(timezone.utc)
    await _seed_prior_sent_send(db_session, c, t, r, sent_at=now - timedelta(hours=25))
    s = await _seed_queued_send(db_session, c, t, r, queued_at=_ANCIENT)

    result = await send_worker.run_once()

    assert result["sent"] == 1
    assert result["throttled"] == 0

    refreshed = await _read_send(s.id)
    assert refreshed.status == "sent"


async def test_run_once_cold_tier_gated_before_7_days(db_session, monkeypatch):
    """Cold tier's gap is 7 days: a send from 3 days ago is still too recent."""
    _settings_override(monkeypatch, send_daily_quota=10_000_000)

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email, engagement_tier=TIER_COLD)
    c, t = await _seed_campaign_template(db_session)
    now = datetime.now(timezone.utc)
    last_sent = now - timedelta(days=3)
    await _seed_prior_sent_send(db_session, c, t, r, sent_at=last_sent)
    s = await _seed_queued_send(db_session, c, t, r, queued_at=_ANCIENT)

    result = await send_worker.run_once()

    assert result["throttled"] == 1
    assert result["sent"] == 0

    refreshed = await _read_send(s.id)
    assert refreshed.status == "queued"
    assert refreshed.failure_reason is None
    assert refreshed.queued_at > now
    expected = last_sent + send_worker.min_gap_for_tier(TIER_COLD)
    assert abs((refreshed.queued_at - expected).total_seconds()) < 2


async def test_run_once_dormant_tier_skipped_and_requeued_far_out(db_session, monkeypatch):
    """Dormant rows are pushed far out here, not suppressed — the nightly
    scorer worker owns suppression for dormant recipients."""
    _settings_override(monkeypatch, send_daily_quota=10_000_000)

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email, engagement_tier=TIER_DORMANT)
    c, t = await _seed_campaign_template(db_session)
    now = datetime.now(timezone.utc)
    s = await _seed_queued_send(db_session, c, t, r, queued_at=_ANCIENT)

    result = await send_worker.run_once()

    assert result["throttled"] == 1
    assert result["sent"] == 0

    refreshed = await _read_send(s.id)
    assert refreshed.status == "queued"
    assert refreshed.failure_reason is None
    assert refreshed.queued_at > now + timedelta(days=300)

    suppression_hit = await db_session.execute(
        select(Suppression).where(Suppression.email == email)
    )
    assert suppression_hit.first() is None


# --- Weekly / monthly frequency caps (SPEC_ADDENDUM_002 §1.1) ---------------


async def test_run_once_weekly_cap_requeues(db_session, monkeypatch):
    _settings_override(
        monkeypatch, send_daily_quota=10_000_000, frequency_cap_per_week=2
    )

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)  # default tier "engaged", 24h gap
    c, t = await _seed_campaign_template(db_session)
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(days=3)
    # Most recent prior send must clear the 24h engagement gap on its own so
    # this test isolates the weekly-cap gate specifically.
    await _seed_prior_sent_send(db_session, c, t, r, sent_at=oldest)
    await _seed_prior_sent_send(db_session, c, t, r, sent_at=now - timedelta(days=2))
    s = await _seed_queued_send(db_session, c, t, r, queued_at=_ANCIENT)

    result = await send_worker.run_once()

    assert result["weekly_capped"] == 1
    assert result["sent"] == 0
    assert result["throttled"] == 0

    refreshed = await _read_send(s.id)
    assert refreshed.status == "queued"
    assert refreshed.failure_reason is None
    assert refreshed.queued_at > now
    expected = oldest + send_worker.WEEKLY_CAP_WINDOW
    assert abs((refreshed.queued_at - expected).total_seconds()) < 2


async def test_run_once_monthly_cap_requeues(db_session, monkeypatch):
    _settings_override(
        monkeypatch, send_daily_quota=10_000_000, frequency_cap_per_month=2
    )

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email)  # default tier "engaged", 24h gap
    c, t = await _seed_campaign_template(db_session)
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(days=15)
    # Both prior sends fall outside the 7-day weekly window (so the weekly
    # gate doesn't trip) but inside the 30-day monthly window.
    await _seed_prior_sent_send(db_session, c, t, r, sent_at=oldest)
    await _seed_prior_sent_send(db_session, c, t, r, sent_at=now - timedelta(days=10))
    s = await _seed_queued_send(db_session, c, t, r, queued_at=_ANCIENT)

    result = await send_worker.run_once()

    assert result["monthly_capped"] == 1
    assert result["sent"] == 0
    assert result["weekly_capped"] == 0
    assert result["throttled"] == 0

    refreshed = await _read_send(s.id)
    assert refreshed.status == "queued"
    assert refreshed.failure_reason is None
    assert refreshed.queued_at > now
    expected = oldest + send_worker.MONTHLY_CAP_WINDOW
    assert abs((refreshed.queued_at - expected).total_seconds()) < 2


# --- Quiet hours (SPEC_ADDENDUM_002 §1.2) ------------------------------------


async def test_run_once_quiet_hours_requeues_to_window_open(db_session, monkeypatch):
    # Restore the real window-math implementation (the autouse fixture stubs
    # it to "always open" for every other test in this module).
    monkeypatch.setattr(send_worker, "next_window_open", _real_next_window_open)

    zone = ZoneInfo("America/Los_Angeles")
    local_now = datetime.now(timezone.utc).astimezone(zone)
    # Pick a 1-hour window guaranteed to exclude the current local hour, so
    # the test is deterministic regardless of when it actually runs.
    if local_now.hour <= 20:
        start_hour, end_hour = local_now.hour + 2, local_now.hour + 3
    else:
        start_hour, end_hour = 0, 1

    _settings_override(
        monkeypatch,
        send_daily_quota=10_000_000,
        send_window_start_hour=start_hour,
        send_window_end_hour=end_hour,
    )

    email = f"test-{uuid.uuid4()}@cadencetests.io"
    r = await _seed_recipient(db_session, email, attrs={"state": "CA"})
    c, t = await _seed_campaign_template(db_session)
    s = await _seed_queued_send(db_session, c, t, r, queued_at=_ANCIENT)

    result = await send_worker.run_once()

    assert result["quiet_hours_skipped"] == 1
    assert result["sent"] == 0

    refreshed = await _read_send(s.id)
    assert refreshed.status == "queued"
    assert refreshed.failure_reason is None
    assert refreshed.queued_at > datetime.now(timezone.utc)
    local_retry = refreshed.queued_at.astimezone(zone)
    assert local_retry.hour == start_hour
