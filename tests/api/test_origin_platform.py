"""Integration tests for POST /ingest/origin-platform.

Covers SPEC-segmentation.md §12 scenarios 4, 7, 8, 8a–8f.

All rows inserted here use emails ending in @cadencetests.io so the autouse
_clean_test_rows fixture in conftest removes recipients. Additional rows in
the new segmentation tables (click_registry, contact_event,
contact_activity_daily, contact_profile) are cleaned up by the per-test
_clean_seg_rows fixture defined below, keyed on the same tenant tag.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import text

from app.core.db import async_session_factory

# Tenant used throughout so the cleanup fixture can wipe by tenant.
_TENANT = "otto_seg_test"

# Fixed "now" used for most tests.
_NOW = datetime(2026, 7, 30, 10, 0, 0, tzinfo=timezone.utc)

# Conversion event_type code (see app/domain/event_codes.py).
_CONVERSION_TYPE: int = 11
# Click event_type code.
_CLICK_TYPE: int = 7

_CLASSIFICATION_HUMAN: int = 0
_CLASSIFICATION_SCANNER: int = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uid() -> str:
    return str(uuid.uuid4())


async def _seed_campaign(db_session) -> str:
    """Insert a campaign row and return its UUID string."""
    from app.models.campaign import Campaign

    c = Campaign(
        tenant=_TENANT,
        name=f"test-campaign-{uuid.uuid4()}",
        vertical="auto",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return str(c.id)


async def _seed_recipient(db_session, email: str | None = None) -> str:
    """Insert a recipient row and return its UUID string."""
    if email is None:
        email = f"seg-test-{uuid.uuid4()}@cadencetests.io"
    rid = _uid()
    now = datetime.now(timezone.utc)
    await db_session.execute(
        text("""
            INSERT INTO recipients
                (id, tenant, email, external_id, vertical,
                 first_seen_at, last_seen_at, consent_source, consent_at, attributes)
            VALUES
                (:id, :tenant, :email, :ext_id, 'auto',
                 :now, :now, 'otto_form', :now, '{}')
        """),
        {
            "id": rid,
            "tenant": _TENANT,
            "email": email,
            "ext_id": f"ext-{rid}",
            "now": now,
        },
    )
    await db_session.commit()
    return rid


async def _seed_consent_proof(db_session, contact_id: str) -> str:
    """Insert a consent_proof row and return its UUID string."""
    cid = _uid()
    await db_session.execute(
        text("""
            INSERT INTO consent_proof
                (consent_proof_id, tenant, contact_id, captured_at,
                 disclosure_text_hash, channels)
            VALUES
                (:cid, :tenant, :contact_id, now(), 'deadbeef', ARRAY[1]::smallint[])
        """),
        {"cid": cid, "tenant": _TENANT, "contact_id": contact_id},
    )
    await db_session.commit()
    return cid


async def _seed_contact_profile(
    db_session,
    contact_id: str,
    consent_proof_id: str,
    *,
    last_click_at: datetime | None = None,
    profile_version: int = 1,
    clicks_30d: int = 0,
) -> None:
    """Insert a minimal contact_profile row for the given contact."""
    await db_session.execute(
        text("""
            INSERT INTO contact_profile
                (contact_id, tenant, channel,
                 consent_state, consent_proof_id,
                 profile_version, tier_config_version, updated_at,
                 last_click_at, clicks_30d)
            VALUES
                (:cid, :tenant, 1,
                 1, :proof_id,
                 :pv, 'test-config-v1', now(),
                 :last_click_at, :clicks_30d)
            ON CONFLICT (tenant, contact_id, channel) DO UPDATE
                SET profile_version = EXCLUDED.profile_version,
                    last_click_at   = EXCLUDED.last_click_at,
                    clicks_30d      = EXCLUDED.clicks_30d
        """),
        {
            "cid": contact_id,
            "tenant": _TENANT,
            "proof_id": consent_proof_id,
            "pv": profile_version,
            "last_click_at": last_click_at,
            "clicks_30d": clicks_30d,
        },
    )
    await db_session.commit()


async def _seed_activity_daily(
    db_session,
    contact_id: str,
    day: "date",
    *,
    clicks: int = 0,
    clicks_bot: int = 0,
) -> None:
    """Insert a contact_activity_daily row. `day` must be a datetime.date object."""
    from datetime import date as date_type
    await db_session.execute(
        text("""
            INSERT INTO contact_activity_daily
                (contact_id, tenant, channel, day, clicks, clicks_bot)
            VALUES
                (:cid, :tenant, 1, :day, :clicks, :clicks_bot)
            ON CONFLICT (tenant, contact_id, channel, day) DO UPDATE
                SET clicks     = EXCLUDED.clicks,
                    clicks_bot = EXCLUDED.clicks_bot
        """),
        {
            "cid": contact_id,
            "tenant": _TENANT,
            "day": day,
            "clicks": clicks,
            "clicks_bot": clicks_bot,
        },
    )
    await db_session.commit()


async def _seed_click_registry(
    db_session,
    click_id: str,
    contact_id: str,
    *,
    campaign_id: str | None = None,
    classification: int = _CLASSIFICATION_HUMAN,
    clicked_at: datetime | None = None,
) -> None:
    """Insert a click_registry row."""
    if clicked_at is None:
        clicked_at = _NOW - timedelta(days=1)
    await db_session.execute(
        text("""
            INSERT INTO click_registry
                (click_id, tenant, contact_id, channel, campaign_id,
                 clicked_at, classification)
            VALUES
                (:click_id, :tenant, :contact_id, 1, :campaign_id,
                 :clicked_at, :classification)
            ON CONFLICT (click_id) DO UPDATE
                SET classification = EXCLUDED.classification,
                    clicked_at     = EXCLUDED.clicked_at,
                    campaign_id    = EXCLUDED.campaign_id
        """),
        {
            "click_id": click_id,
            "tenant": _TENANT,
            "contact_id": contact_id,
            "campaign_id": campaign_id,
            "clicked_at": clicked_at,
            "classification": classification,
        },
    )
    await db_session.commit()


async def _seed_contact_event(
    db_session,
    contact_id: str,
    *,
    click_id: str | None = None,
    event_type: int = _CLICK_TYPE,
    classification: int = _CLASSIFICATION_HUMAN,
    occurred_at: datetime | None = None,
    campaign_id: str | None = None,
) -> str:
    """Insert a contact_event row and return its event_id."""
    if occurred_at is None:
        occurred_at = _NOW - timedelta(days=1)
    eid = _uid()
    await db_session.execute(
        text("""
            INSERT INTO contact_event
                (event_id, tenant, contact_id, channel, event_type,
                 occurred_at, source, click_id, classification, campaign_id)
            VALUES
                (:eid, :tenant, :contact_id, 1, :event_type,
                 :occurred_at, 2, :click_id, :classification, :campaign_id)
        """),
        {
            "eid": eid,
            "tenant": _TENANT,
            "contact_id": contact_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "click_id": click_id,
            "classification": classification,
            "campaign_id": campaign_id,
        },
    )
    await db_session.commit()
    return eid


def _event_payload(
    event_type: str,
    click_id: str,
    *,
    tenant: str = _TENANT,
    occurred_at: datetime | None = None,
    campaign_id: str | None = None,
) -> dict:
    if occurred_at is None:
        occurred_at = _NOW
    body: dict = {
        "event_type": event_type,
        "click_id": click_id,
        "tenant": tenant,
        "occurred_at": occurred_at.isoformat(),
    }
    if campaign_id is not None:
        body["campaign_id"] = campaign_id
    return body


# ---------------------------------------------------------------------------
# Cleanup fixture — runs before and after each test
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def _clean_seg_rows():
    async def _purge() -> None:
        async with async_session_factory() as s:
            # Order matters: child tables before parents.
            await s.execute(
                text("DELETE FROM contact_event WHERE tenant = :t"),
                {"t": _TENANT},
            )
            await s.execute(
                text("DELETE FROM click_registry WHERE tenant = :t"),
                {"t": _TENANT},
            )
            await s.execute(
                text("DELETE FROM contact_activity_daily WHERE tenant = :t"),
                {"t": _TENANT},
            )
            await s.execute(
                text("DELETE FROM contact_profile WHERE tenant = :t"),
                {"t": _TENANT},
            )
            await s.execute(
                text("DELETE FROM consent_proof WHERE tenant = :t"),
                {"t": _TENANT},
            )
            # Recipients cleaned last (FK target for most tables).
            await s.execute(
                text("DELETE FROM recipients WHERE tenant = :t"),
                {"t": _TENANT},
            )
            # Campaigns seeded for FK satisfaction in conversion tests.
            await s.execute(
                text("DELETE FROM campaigns WHERE tenant = :t"),
                {"t": _TENANT},
            )
            await s.commit()

    await _purge()
    yield
    await _purge()


# ---------------------------------------------------------------------------
# Scenario 4: origin-platform click confirmation reclassifies scanner→human
# ---------------------------------------------------------------------------

async def test_scenario_4_origin_click_reclassifies_scanner(client, db_session) -> None:
    """Scenario 4: SES Click + origin-platform click, same click_id.

    Insert a click_registry row with classification=scanner (2).
    POST event_type=click to /ingest/origin-platform with the same click_id.
    Assert click_registry.classification = 0 (human) and reclassified_at is set.
    """
    contact_id = await _seed_recipient(db_session)
    click_id = f"click-{_uid()}"
    await _seed_click_registry(
        db_session, click_id, contact_id, classification=_CLASSIFICATION_SCANNER
    )

    resp = await client.post(
        "/ingest/origin-platform",
        json=_event_payload("click", click_id),
    )
    assert resp.status_code == 200, resp.text

    async with async_session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT classification, reclassified_at "
                    "FROM click_registry WHERE click_id = :click_id"
                ),
                {"click_id": click_id},
            )
        ).mappings().one()

    assert row["classification"] == _CLASSIFICATION_HUMAN
    assert row["reclassified_at"] is not None


# ---------------------------------------------------------------------------
# Scenario 7: origin-platform click for unknown click_id → graceful no-op
# ---------------------------------------------------------------------------

async def test_scenario_7_unknown_click_id_is_graceful_noop(client) -> None:
    """Scenario 7: click_id not in click_registry → HTTP 200, no error."""
    resp = await client.post(
        "/ingest/origin-platform",
        json=_event_payload("click", f"nonexistent-{_uid()}"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Scenario 8: conversion → contact_event inserted, click_registry updated
# ---------------------------------------------------------------------------

async def test_scenario_8_conversion_records_contact_event(client, db_session) -> None:
    """Scenario 8: POST event_type=conversion → contact_event row with event_type=11,
    click_registry.converted_at IS NOT NULL, click_registry.convert_count = 1.
    """
    contact_id = await _seed_recipient(db_session)
    click_id = f"click-{_uid()}"
    campaign_id = await _seed_campaign(db_session)

    await _seed_click_registry(
        db_session, click_id, contact_id, campaign_id=campaign_id
    )

    resp = await client.post(
        "/ingest/origin-platform",
        json=_event_payload("conversion", click_id),
    )
    assert resp.status_code == 200, resp.text

    async with async_session_factory() as s:
        ce_row = (
            await s.execute(
                text(
                    "SELECT event_type FROM contact_event "
                    "WHERE click_id = :click_id AND event_type = :et AND tenant = :t"
                ),
                {"click_id": click_id, "et": _CONVERSION_TYPE, "t": _TENANT},
            )
        ).mappings().one()

        cr_row = (
            await s.execute(
                text(
                    "SELECT converted_at, convert_count "
                    "FROM click_registry WHERE click_id = :click_id"
                ),
                {"click_id": click_id},
            )
        ).mappings().one()

    assert ce_row["event_type"] == _CONVERSION_TYPE
    assert cr_row["converted_at"] is not None
    assert cr_row["convert_count"] == 1


# ---------------------------------------------------------------------------
# Scenario 8a: conversion not marked dedup_of the originating click
# ---------------------------------------------------------------------------

async def test_scenario_8a_conversion_not_dedup_of_click(client, db_session) -> None:
    """Scenario 8a: same click_id on click and conversion; conversion dedup_of IS NULL.

    The dedup key is (click_id, event_type) — since click and conversion have
    different event_types they must both persist. The conversion row's dedup_of
    must NOT be set to the click row's event_id.
    """
    contact_id = await _seed_recipient(db_session)
    click_id = f"click-{_uid()}"

    # Pre-existing click contact_event row.
    await _seed_contact_event(
        db_session, contact_id, click_id=click_id, event_type=_CLICK_TYPE
    )
    await _seed_click_registry(db_session, click_id, contact_id)

    resp = await client.post(
        "/ingest/origin-platform",
        json=_event_payload("conversion", click_id),
    )
    assert resp.status_code == 200, resp.text

    async with async_session_factory() as s:
        conv_row = (
            await s.execute(
                text(
                    "SELECT dedup_of FROM contact_event "
                    "WHERE click_id = :click_id AND event_type = :et AND tenant = :t"
                ),
                {"click_id": click_id, "et": _CONVERSION_TYPE, "t": _TENANT},
            )
        ).mappings().one()

    # dedup_of must NOT be set — conversion is a distinct event type from click.
    assert conv_row["dedup_of"] is None


# ---------------------------------------------------------------------------
# Scenario 8b: click outside attribution window → unattributed, campaign_id NULL
# ---------------------------------------------------------------------------

async def test_scenario_8b_outside_attribution_window_is_unattributed(
    client, db_session
) -> None:
    """Scenario 8b: click is 45 days old, attribution_window_days=30 (default).

    Conversion is recorded unattributed: contact_event inserted with campaign_id=NULL.
    HTTP 200, no error.
    """
    contact_id = await _seed_recipient(db_session)
    click_id = f"click-{_uid()}"
    campaign_id = await _seed_campaign(db_session)
    old_click_at = _NOW - timedelta(days=45)

    await _seed_click_registry(
        db_session,
        click_id,
        contact_id,
        campaign_id=campaign_id,
        clicked_at=old_click_at,
    )

    resp = await client.post(
        "/ingest/origin-platform",
        json=_event_payload("conversion", click_id, occurred_at=_NOW),
    )
    assert resp.status_code == 200, resp.text

    async with async_session_factory() as s:
        ce_row = (
            await s.execute(
                text(
                    "SELECT campaign_id FROM contact_event "
                    "WHERE click_id = :click_id AND event_type = :et AND tenant = :t"
                ),
                {"click_id": click_id, "et": _CONVERSION_TYPE, "t": _TENANT},
            )
        ).mappings().one()

    # Unattributed: campaign_id must be NULL.
    assert ce_row["campaign_id"] is None


# ---------------------------------------------------------------------------
# Scenario 8c: unresolvable click_id → unattributed, HTTP 200
# ---------------------------------------------------------------------------

async def test_scenario_8c_unresolvable_click_id_is_unattributed(client) -> None:
    """Scenario 8c: click_id not in click_registry → HTTP 200, no error, logged.

    When the click_id is unresolvable the handler cannot determine contact_id, so
    no contact_event is inserted (contact_event.contact_id is NOT NULL). The SPEC
    requirement is "no error; logged" — we verify HTTP 200 and a successful response
    body. The event is recorded as a log entry rather than a DB row when contact is
    unknown, because the FK constraint on contact_id prevents a NULL insert.
    """
    ghost_click_id = f"ghost-{_uid()}"

    resp = await client.post(
        "/ingest/origin-platform",
        json=_event_payload("conversion", ghost_click_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Scenario 8d: last-touch attribution — most recent click in window wins
# ---------------------------------------------------------------------------

async def test_scenario_8d_last_touch_attribution(client, db_session) -> None:
    """Scenario 8d: two click_registry rows for same campaign; only the more
    recent one gets convert_count incremented.

    Last-touch attribution: the most recent click inside the window wins.
    """
    contact_id = await _seed_recipient(db_session)
    campaign_id = await _seed_campaign(db_session)

    click_id_old = f"click-old-{_uid()}"
    click_id_new = f"click-new-{_uid()}"

    old_clicked_at = _NOW - timedelta(days=10)
    new_clicked_at = _NOW - timedelta(days=2)

    await _seed_click_registry(
        db_session, click_id_old, contact_id,
        campaign_id=campaign_id, clicked_at=old_clicked_at,
    )
    await _seed_click_registry(
        db_session, click_id_new, contact_id,
        campaign_id=campaign_id, clicked_at=new_clicked_at,
    )

    # Conversion references the more recent click_id (last-touch).
    resp = await client.post(
        "/ingest/origin-platform",
        json=_event_payload("conversion", click_id_new, occurred_at=_NOW),
    )
    assert resp.status_code == 200, resp.text

    async with async_session_factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT click_id, convert_count "
                    "FROM click_registry "
                    "WHERE click_id = ANY(:ids) "
                    "ORDER BY clicked_at DESC"
                ),
                {"ids": [click_id_old, click_id_new]},
            )
        ).mappings().all()

    by_id = {r["click_id"]: r["convert_count"] for r in rows}
    # Last-touch row: convert_count = 1.
    assert by_id[click_id_new] == 1, "most recent click must get convert_count = 1"
    # Earlier row: untouched.
    assert by_id[click_id_old] == 0, "earlier click must remain convert_count = 0"


# ---------------------------------------------------------------------------
# Scenario 8e: scanner click that converts → reclassified; counters corrected
# ---------------------------------------------------------------------------

async def test_scenario_8e_scanner_click_converts_reclassifies(
    client, db_session
) -> None:
    """Scenario 8e: click_registry.classification=2, POST conversion.

    Assert:
    - click_registry.classification = 0 (human)
    - contact_activity_daily.clicks_bot decremented on the original click's day
    - contact_activity_daily.clicks incremented on the same day
    - contact_profile.profile_version bumped
    """
    contact_id = await _seed_recipient(db_session)
    proof_id = await _seed_consent_proof(db_session, contact_id)
    click_id = f"click-{_uid()}"
    click_day = (_NOW - timedelta(days=3)).date()
    clicked_at = datetime(
        click_day.year, click_day.month, click_day.day,
        10, 0, 0, tzinfo=timezone.utc
    )

    await _seed_click_registry(
        db_session, click_id, contact_id,
        classification=_CLASSIFICATION_SCANNER,
        clicked_at=clicked_at,
    )
    await _seed_activity_daily(
        db_session, contact_id, click_day,
        clicks=0, clicks_bot=1,
    )
    initial_version = 5
    await _seed_contact_profile(
        db_session, contact_id, proof_id, profile_version=initial_version
    )

    resp = await client.post(
        "/ingest/origin-platform",
        json=_event_payload("conversion", click_id, occurred_at=_NOW),
    )
    assert resp.status_code == 200, resp.text

    async with async_session_factory() as s:
        cr = (
            await s.execute(
                text(
                    "SELECT classification FROM click_registry "
                    "WHERE click_id = :click_id"
                ),
                {"click_id": click_id},
            )
        ).mappings().one()

        daily = (
            await s.execute(
                text(
                    "SELECT clicks, clicks_bot "
                    "FROM contact_activity_daily "
                    "WHERE contact_id = :cid AND day = :day AND tenant = :t"
                ),
                {"cid": contact_id, "day": click_day, "t": _TENANT},
            )
        ).mappings().one()

        profile = (
            await s.execute(
                text(
                    "SELECT profile_version FROM contact_profile "
                    "WHERE contact_id = :cid AND tenant = :t"
                ),
                {"cid": contact_id, "t": _TENANT},
            )
        ).mappings().one()

    assert cr["classification"] == _CLASSIFICATION_HUMAN
    assert daily["clicks"] == 1, "clicks must be incremented on the original click day"
    assert daily["clicks_bot"] == 0, "clicks_bot must be decremented on the original click day"
    assert profile["profile_version"] > initial_version, "profile_version must be bumped"


# ---------------------------------------------------------------------------
# Scenario 8f: reclassified click predates current last_click_at → unchanged
# ---------------------------------------------------------------------------

async def test_scenario_8f_reclassified_click_predates_last_click_at(
    client, db_session
) -> None:
    """Scenario 8f: click_registry.classification=2, and contact_profile.last_click_at
    is already more recent than the original click.

    POST conversion. Assert:
    - last_click_at unchanged (GREATEST is preserved — older reclassified click
      does not overwrite a more recent confirmed click)
    - counters still corrected (clicks_bot -= 1, clicks += 1 on the original day)
    """
    contact_id = await _seed_recipient(db_session)
    proof_id = await _seed_consent_proof(db_session, contact_id)
    click_id = f"click-{_uid()}"

    click_day = (_NOW - timedelta(days=10)).date()
    clicked_at = datetime(
        click_day.year, click_day.month, click_day.day,
        10, 0, 0, tzinfo=timezone.utc
    )
    # last_click_at is MORE RECENT than the scanner click.
    later_click_at = _NOW - timedelta(days=2)

    await _seed_click_registry(
        db_session, click_id, contact_id,
        classification=_CLASSIFICATION_SCANNER,
        clicked_at=clicked_at,
    )
    await _seed_activity_daily(
        db_session, contact_id, click_day,
        clicks=0, clicks_bot=1,
    )
    await _seed_contact_profile(
        db_session, contact_id, proof_id,
        last_click_at=later_click_at,
        profile_version=3,
    )

    resp = await client.post(
        "/ingest/origin-platform",
        json=_event_payload("conversion", click_id, occurred_at=_NOW),
    )
    assert resp.status_code == 200, resp.text

    async with async_session_factory() as s:
        profile = (
            await s.execute(
                text(
                    "SELECT last_click_at, profile_version "
                    "FROM contact_profile "
                    "WHERE contact_id = :cid AND tenant = :t"
                ),
                {"cid": contact_id, "t": _TENANT},
            )
        ).mappings().one()

        daily = (
            await s.execute(
                text(
                    "SELECT clicks, clicks_bot "
                    "FROM contact_activity_daily "
                    "WHERE contact_id = :cid AND day = :day AND tenant = :t"
                ),
                {"cid": contact_id, "day": click_day, "t": _TENANT},
            )
        ).mappings().one()

    # last_click_at must remain the more recent later_click_at, not be replaced
    # by the older reclassified click_at.
    assert profile["last_click_at"].replace(tzinfo=timezone.utc) == later_click_at, (
        "last_click_at must not move backward when reclassified click predates it"
    )

    # Counters on the original day must still be corrected.
    assert daily["clicks"] == 1, "clicks must be incremented even when last_click_at unchanged"
    assert daily["clicks_bot"] == 0, "clicks_bot must still be decremented"
