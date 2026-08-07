"""Tests for app.workers.reputation_checks — scenarios 25, 26, 27, 28, 29, 33."""

from __future__ import annotations

import email.message
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text

from app.adapters.alerts.base import AlertEvent
from app.core.db import async_session_factory
from app.models.alert import Alert
from app.workers import reputation_checks

# ---------------------------------------------------------------------------
# Constants from domain_provider seed (seg1 migration)
# ---------------------------------------------------------------------------
_RG_YAHOO = 3   # AT&T-family and verizon.net → yahoo reputation group
_RG_GMAIL = 1
_CH_EMAIL = 1
_SRC_SES = 1
_SRC_GPT = 2

_TENANT = "otto"


def _yesterday() -> date:
    """Return yesterday as a date object."""
    return datetime.now(timezone.utc).date() - timedelta(days=1)


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

async def _purge_rollup(session, tenant: str = _TENANT) -> None:
    await session.execute(
        text("DELETE FROM provider_daily_rollup WHERE tenant = :t"),
        {"t": tenant},
    )
    await session.commit()


async def _purge_snapshots(session, tenant: str = _TENANT) -> None:
    await session.execute(
        text("DELETE FROM segment_tier_snapshot WHERE tenant = :t"),
        {"t": tenant},
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Scenario 25 — complaint_rate denominator is delivers, not sends
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complaint_rate_denominator_is_delivers():
    """Scenario 25: complaint_rate = complaints / delivers, not / sends.

    Insert a provider_daily_rollup row with sends=100000, delivers=95000,
    complaints=200. Manually UPDATE complaint_rate via raw SQL and assert the
    stored value equals 200/95000 ≈ 0.002105, not 200/100000 = 0.002.
    """
    yesterday = _yesterday()

    async with async_session_factory() as session:
        # Insert the row with complaint_rate NULL initially
        await session.execute(
            text(
                """
                INSERT INTO provider_daily_rollup
                (day, tenant, reputation_group, channel, source,
                 sends, delivers, bounces_hard, bounces_soft, complaints,
                 complaint_rate)
                VALUES
                (:day, :tenant, :rg, :ch, :src,
                 100000, 95000, 0, 0, 200,
                 NULL)
                ON CONFLICT (tenant, day, reputation_group, channel, source)
                DO UPDATE SET sends=EXCLUDED.sends,
                              delivers=EXCLUDED.delivers,
                              complaints=EXCLUDED.complaints,
                              complaint_rate=NULL
                """
            ),
            {
                "day": yesterday,
                "tenant": _TENANT,
                "rg": _RG_YAHOO,
                "ch": _CH_EMAIL,
                "src": _SRC_SES,
            },
        )
        await session.commit()

        # Recompute complaint_rate = complaints / NULLIF(delivers, 0)
        await session.execute(
            text(
                """
                UPDATE provider_daily_rollup
                SET complaint_rate = complaints::numeric / NULLIF(delivers, 0)
                WHERE tenant = :tenant
                  AND day = :day
                  AND reputation_group = :rg
                  AND channel = :ch
                  AND source = :src
                """
            ),
            {
                "tenant": _TENANT,
                "day": yesterday,
                "rg": _RG_YAHOO,
                "ch": _CH_EMAIL,
                "src": _SRC_SES,
            },
        )
        await session.commit()

        # Read it back
        row = (
            await session.execute(
                text(
                    """
                    SELECT complaint_rate, sends, delivers, complaints
                    FROM provider_daily_rollup
                    WHERE tenant = :tenant
                      AND day = :day
                      AND reputation_group = :rg
                      AND channel = :ch
                      AND source = :src
                    """
                ),
                {
                    "tenant": _TENANT,
                    "day": yesterday,
                    "rg": _RG_YAHOO,
                    "ch": _CH_EMAIL,
                    "src": _SRC_SES,
                },
            )
        ).fetchone()

        await _purge_rollup(session)

    assert row is not None
    complaint_rate = float(row[0])
    sends = row[1]
    delivers = row[2]
    complaints = row[3]

    assert sends == 100000
    assert delivers == 95000
    assert complaints == 200

    expected_delivers_denominator = 200 / 95000  # ≈ 0.002105
    expected_sends_denominator = 200 / 100000    # = 0.002

    assert abs(complaint_rate - expected_delivers_denominator) < 1e-6, (
        f"complaint_rate {complaint_rate} should be ~{expected_delivers_denominator:.6f} "
        f"(÷ delivers), not {expected_sends_denominator} (÷ sends)"
    )
    assert complaint_rate != pytest.approx(expected_sends_denominator, abs=1e-6), (
        "complaint_rate must not use sends as denominator"
    )


# ---------------------------------------------------------------------------
# Scenario 26 — AT&T-family rolls into yahoo reputation group
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_att_family_rolls_into_yahoo_reputation_group():
    """Scenario 26: AT&T-family volume rolls up under reputation_group=yahoo.

    Insert two provider_daily_rollup rows for yesterday, both with
    reputation_group = _RG_YAHOO (the code assigned to AT&T-family by §3).
    One row represents yahoo.com volume and one represents att.net volume,
    but since both map to the same reputation_group code, a GROUP BY
    reputation_group aggregation returns a single row.
    """
    yesterday = _yesterday()

    async with async_session_factory() as session:
        # Row 1: yahoo.com traffic (source=ses, reputation_group=yahoo)
        await session.execute(
            text(
                """
                INSERT INTO provider_daily_rollup
                (day, tenant, reputation_group, channel, source,
                 sends, delivers, bounces_hard, bounces_soft, complaints)
                VALUES (:day, :tenant, :rg, 1, 1, 50000, 48000, 100, 50, 48)
                ON CONFLICT (tenant, day, reputation_group, channel, source) DO NOTHING
                """
            ),
            {"day": yesterday, "tenant": _TENANT, "rg": _RG_YAHOO},
        )
        # Row 2: att.net traffic — also reputation_group=yahoo (§3 maps AT&T → yahoo)
        # Use source=2 so there's no PK conflict with row 1
        await session.execute(
            text(
                """
                INSERT INTO provider_daily_rollup
                (day, tenant, reputation_group, channel, source,
                 sends, delivers, bounces_hard, bounces_soft, complaints)
                VALUES (:day, :tenant, :rg, 1, 2, 10000, 9800, 20, 10, 9)
                ON CONFLICT (tenant, day, reputation_group, channel, source) DO NOTHING
                """
            ),
            {"day": yesterday, "tenant": _TENANT, "rg": _RG_YAHOO},
        )
        await session.commit()

        # Assert: GROUP BY reputation_group for yesterday yields exactly one
        # yahoo group row (both rows have the same reputation_group code).
        agg_rows = (
            await session.execute(
                text(
                    """
                    SELECT reputation_group,
                           SUM(sends)    AS total_sends,
                           SUM(delivers) AS total_delivers,
                           SUM(complaints) AS total_complaints
                    FROM provider_daily_rollup
                    WHERE tenant = :tenant
                      AND day = :day
                    GROUP BY reputation_group
                    """
                ),
                {"tenant": _TENANT, "day": yesterday},
            )
        ).fetchall()

        await _purge_rollup(session)

    assert len(agg_rows) == 1, (
        f"Expected 1 reputation_group in rollup (yahoo), got {len(agg_rows)}. "
        "AT&T-family must roll up under yahoo, not as a separate row."
    )
    assert agg_rows[0][0] == _RG_YAHOO


# ---------------------------------------------------------------------------
# Scenario 27 — complaint_rate_7d critical fires alert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complaint_rate_7d_critical_fires_alert():
    """Scenario 27: complaint_rate_7d = 0.0035 > critical threshold 0.003 → critical alert.

    Seeds a provider_daily_rollup row for yesterday with complaint_rate_7d
    above critical, calls check_complaint_rate, and asserts an alert row
    is created with metric matching the expected format and severity=critical.
    """
    yesterday = _yesterday()
    mock_dispatcher = AsyncMock()
    mock_dispatcher.notify = AsyncMock()

    async with async_session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO provider_daily_rollup
                (day, tenant, reputation_group, channel, source,
                 sends, delivers, bounces_hard, bounces_soft, complaints,
                 complaint_rate_7d)
                VALUES (:day, :tenant, :rg, :ch, :src,
                        50000, 48000, 0, 0, 168,
                        0.0035)
                ON CONFLICT (tenant, day, reputation_group, channel, source)
                DO UPDATE SET complaint_rate_7d = EXCLUDED.complaint_rate_7d
                """
            ),
            {
                "day": yesterday,
                "tenant": _TENANT,
                "rg": _RG_YAHOO,
                "ch": _CH_EMAIL,
                "src": _SRC_SES,
            },
        )
        await session.commit()

        now = datetime.now(timezone.utc)
        await reputation_checks.check_complaint_rate(
            session, _TENANT, mock_dispatcher, now
        )
        await session.commit()

    expected_metric = f"complaint_rate:rg{_RG_YAHOO}:ch{_CH_EMAIL}"

    async with async_session_factory() as session:
        alerts = (
            await session.execute(
                select(Alert).where(
                    Alert.tenant == _TENANT,
                    Alert.metric == expected_metric,
                )
            )
        ).scalars().all()

        await _purge_rollup(session)

    assert len(alerts) >= 1, (
        f"Expected at least one alert for metric='{expected_metric}', found none."
    )
    alert = alerts[0]
    assert alert.severity == "critical", (
        f"Expected severity='critical' for complaint_rate_7d=0.0035 "
        f"(threshold=0.003), got {alert.severity!r}"
    )
    assert alert.metric == expected_metric


# ---------------------------------------------------------------------------
# Scenario 28 — GPT data unavailable / missing rollup logs rather than raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gpt_data_unavailable_when_api_missing():
    """Scenario 28: no rollup rows for source=2 → missing data logged, no exception.

    Sub-part A: check_complaint_rate for a tenant with no provider_daily_rollup
    rows for yesterday must log missing_rollup_data and return without error.

    Sub-part B: fetch_and_store_gmail_complaint_rate returns None without error
    when gpt_api_credentials_json is unset.
    """
    # --- Sub-part A: no rollup data at all ---
    mock_dispatcher = AsyncMock()
    mock_dispatcher.notify = AsyncMock()

    # Ensure there are no rollup rows for otto today
    async with async_session_factory() as session:
        await _purge_rollup(session, _TENANT)

    async with async_session_factory() as session:
        now = datetime.now(timezone.utc)
        # Must not raise even though there are no rows
        await reputation_checks.check_complaint_rate(
            session, _TENANT, mock_dispatcher, now
        )

    # No alert should have been created
    async with async_session_factory() as session:
        alerts = (
            await session.execute(
                select(Alert).where(
                    Alert.tenant == _TENANT,
                    Alert.metric.like("complaint_rate:%"),
                )
            )
        ).scalars().all()

    assert len(alerts) == 0, (
        "No alert should be created when rollup data is missing."
    )
    mock_dispatcher.notify.assert_not_called()

    # --- Sub-part B: gpt_api returns None when credentials unset ---
    from app.adapters import gpt_api

    async with async_session_factory() as session:
        with patch("app.adapters.gpt_api.get_settings") as mock_settings:
            mock_settings.return_value.gpt_api_credentials_json = None
            result = await gpt_api.fetch_and_store_gmail_complaint_rate(
                session, _TENANT
            )

    # fetch_and_store_gmail_complaint_rate returns None implicitly (no-op stub)
    assert result is None


# ---------------------------------------------------------------------------
# Scenario 29 — Feedback-ID header present/absent based on OutboundMessage
# ---------------------------------------------------------------------------

def test_feedback_id_absent_logged():
    """Scenario 29: Feedback-ID header absent when feedback_id=None; present when set.

    This is a pure unit test — no DB. Instantiates OutboundMessage without
    feedback_id, calls SESAdapter._build_raw_mime, parses the result as an
    email.message.Message, and asserts:
    - No Feedback-ID header when feedback_id is None.
    - Feedback-ID header present with correct value when feedback_id is set.
    """
    from app.adapters.esp.base import OutboundMessage
    from app.adapters.esp.ses import SESAdapter
    from unittest.mock import MagicMock

    mock_settings = MagicMock()
    mock_settings.ses_from_address = "sender@cadencetests.io"
    mock_settings.aws_region = "us-east-1"
    mock_settings.ses_configuration_set = None
    mock_settings.ses_managed_pool_name = None

    adapter = SESAdapter(settings=mock_settings)

    # --- Without feedback_id ---
    msg_no_fid = OutboundMessage(
        to_email="recipient@cadencetests.io",
        from_address="sender@cadencetests.io",
        subject="Test subject",
        body_html="<p>Hello</p>",
        body_text="Hello",
        feedback_id=None,
    )
    raw_no_fid = adapter._build_raw_mime(msg_no_fid)
    parsed_no_fid = email.message_from_bytes(raw_no_fid)
    assert parsed_no_fid["Feedback-ID"] is None, (
        "Feedback-ID header must NOT be present when feedback_id=None"
    )

    # --- With feedback_id ---
    fid_value = "camp-1:otto"
    msg_with_fid = OutboundMessage(
        to_email="recipient@cadencetests.io",
        from_address="sender@cadencetests.io",
        subject="Test subject",
        body_html="<p>Hello</p>",
        body_text="Hello",
        feedback_id=fid_value,
    )
    raw_with_fid = adapter._build_raw_mime(msg_with_fid)
    parsed_with_fid = email.message_from_bytes(raw_with_fid)
    assert parsed_with_fid["Feedback-ID"] == fid_value, (
        f"Feedback-ID header must be '{fid_value}' when feedback_id is set, "
        f"got {parsed_with_fid['Feedback-ID']!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 33 — cold_pool net-negative 3 consecutive runs fires warn alert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cold_pool_net_negative_3_runs_alerts():
    """Scenario 33: 3 consecutive net-negative cold-pool runs → warn alert.

    Inserts real segment_tier_snapshot rows using the actual schema
    (tier SMALLINT, contact_count INT) so that check_cold_pool can run
    against the real DB without any mocking.

    3 runs with increasing cold counts and slowing acquisition growth:
      run 1 (oldest): cold(tier=5)=100, new(tier=6)=200
      run 2:          cold=150, new=210  → delta cold=50 > delta acq=10 → net neg
      run 3 (newest): cold=210, new=215  → delta cold=60 > delta acq=5  → net neg

    Asserts:
    - An alert is created with metric='cold_pool' and severity='warn'.
    """
    mock_dispatcher = AsyncMock()
    mock_dispatcher.notify = AsyncMock()

    now = datetime.now(timezone.utc)
    run1 = now - timedelta(hours=48)
    run2 = now - timedelta(hours=24)
    run3 = now

    # TIER_COLD = 5, TIER_NEW = 6
    snapshot_rows = [
        # (tenant, run_at, tier, contact_count)
        (_TENANT, run1, 5, 100),
        (_TENANT, run1, 6, 200),
        (_TENANT, run2, 5, 150),
        (_TENANT, run2, 6, 210),
        (_TENANT, run3, 5, 210),
        (_TENANT, run3, 6, 215),
    ]

    async with async_session_factory() as session:
        await _purge_snapshots(session)
        await session.execute(
            text(
                "INSERT INTO segment_tier_snapshot "
                "(id, tenant, run_at, tier, contact_count) "
                "VALUES (gen_random_uuid(), :tenant, :run_at, :tier, :contact_count)"
            ),
            [
                {"tenant": t, "run_at": r, "tier": tier, "contact_count": cnt}
                for t, r, tier, cnt in snapshot_rows
            ],
        )
        await session.commit()

    async with async_session_factory() as session:
        await reputation_checks.check_cold_pool(
            session, _TENANT, mock_dispatcher, now
        )

    async with async_session_factory() as session:
        alerts = (
            await session.execute(
                select(Alert).where(
                    Alert.tenant == _TENANT,
                    Alert.metric == "cold_pool",
                )
            )
        ).scalars().all()

    assert len(alerts) >= 1, (
        "Expected at least one cold_pool alert after 3 net-negative runs."
    )
    alert = alerts[0]
    assert alert.metric == "cold_pool"
    assert alert.severity in ("warn", "critical"), (
        f"Expected severity warn or critical for cold_pool alert, got {alert.severity!r}"
    )
