"""Send worker: pulls queued sends, renders, rate-limits, and dispatches via ESP.

Per SPEC §5 (rendering), §10 (compliance — suppression check at send time,
unsub link + List-Unsubscribe headers), §13 (per-second rate limit, per-day
account quota, per-recipient 24h throttle). Per SPEC_ADDENDUM.md §2.2, no
warmup/IP-selection logic belongs here — SES Managed Dedicated IPs handles
that.

Per SPEC_ADDENDUM_002 §1-2, four batched gates run per claimed send, in
order: engagement-gap (subsumes the old flat 24h throttle) -> weekly cap ->
monthly cap -> quiet hours -> the pre-existing suppression/quota/rate-limit
checks. Every gate is a single query for the whole batch, never per-send;
ineligible sends are re-queued (queued_at pushed forward), never failed.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select, text, update

from app.adapters.esp.base import OutboundMessage
from app.adapters.esp.factory import get_esp_adapter
from app.core.config import get_settings
from app.core.db import async_session_factory
from app.core.unsub import UnsubPayload, get_unsub_signer, unsub_base_for_identity
from app.domain.engagement import TIER_DORMANT, min_gap_for_tier
from app.domain.rendering import MissingMergeTag, render_template
from app.domain.timezone import next_window_open, timezone_for_state
from app.models.recipient import Recipient
from app.models.send import Send
from app.models.sending_identity import SendingIdentity
from app.models.template import Template

log = structlog.get_logger(__name__)

# --- Token bucket (module-level) --------------------------------------------

_tokens: float = 0.0
_last_refill: float = time.monotonic()
_lock = asyncio.Lock()


async def _take_token() -> None:
    """Block until one token is available, then consume it.

    The lock is held only while we mutate the bucket; sleeps happen
    outside the lock so concurrent callers can refill in parallel.
    """
    global _tokens, _last_refill
    while True:
        async with _lock:
            rate = max(get_settings().send_rate_limit_per_second, 1)
            now = time.monotonic()
            elapsed = now - _last_refill
            _tokens = min(float(rate), _tokens + elapsed * rate)
            _last_refill = now
            if _tokens >= 1:
                _tokens -= 1
                return
        # Sleep outside the lock so other workers can refill.
        await asyncio.sleep(1.0 / rate)


# --- Guards -------------------------------------------------------------

RECIPIENT_THROTTLE_WINDOW = timedelta(hours=24)
WEEKLY_CAP_WINDOW = timedelta(days=7)
MONTHLY_CAP_WINDOW = timedelta(days=30)


# --- Helpers ----------------------------------------------------------------


def _inject_unsub_html(body_html: str, url: str) -> str:
    if url in body_html:
        return body_html
    return body_html + f'\n<p><a href="{url}">Unsubscribe</a></p>'


def _inject_unsub_text(body_text: str, url: str) -> str:
    if url in body_text:
        return body_text
    return body_text + f"\n\nUnsubscribe: {url}\n"


# --- Worker ------------------------------------------------------------------


def _empty_result() -> dict[str, int]:
    """Zeroed result dict, shared by every early-exit path in `run_once`."""
    return {
        "processed": 0,
        "sent": 0,
        "suppressed": 0,
        "failed": 0,
        "throttled": 0,
        "weekly_capped": 0,
        "monthly_capped": 0,
        "quiet_hours_skipped": 0,
    }


async def run_once() -> dict[str, int]:
    settings = get_settings()
    batch_size = max(settings.send_rate_limit_per_second, 1)

    processed = 0
    sent = 0
    suppressed = 0
    failed = 0
    throttled = 0
    weekly_capped = 0
    monthly_capped = 0
    quiet_hours_skipped = 0

    signer = get_unsub_signer()

    now = datetime.now(timezone.utc)

    # (0) Per-day account quota. Counted once per cycle (not per-send) from
    # `sent_at`, which is set the moment we hand a message to the ESP and is
    # never cleared by later status transitions (delivered/bounced/etc.), so
    # it's an accurate count of SES API calls made today regardless of a
    # send's current status. The quota is account-wide (SES enforces it per
    # AWS account), so this intentionally does not filter by tenant.
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with async_session_factory() as session:
        sent_today = (
            await session.execute(
                select(func.count())
                .select_from(Send)
                .where(Send.sent_at >= today_start)
            )
        ).scalar_one()

    quota_remaining = settings.send_daily_quota - sent_today
    if quota_remaining <= 0:
        log.warning(
            "send_worker.quota_exhausted",
            sent_today=sent_today,
            daily_quota=settings.send_daily_quota,
        )
        return _empty_result()

    # Cap the batch to whatever quota remains so we never claim more sends
    # than we're allowed to dispatch this cycle; the leftovers stay queued
    # and are picked up once the UTC day rolls over.
    batch_size = min(batch_size, quota_remaining)

    # Phase 1: lock a batch, flip queued→sending, commit. That removes the
    # batch from the queued pool so a second concurrent worker can't re-pick
    # it while this worker iterates row-by-row.
    async with async_session_factory() as session:
        stmt = (
            select(Send.id)
            .where(Send.status == "queued", Send.queued_at <= now)
            .order_by(Send.queued_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        ids = list((await session.execute(stmt)).scalars().all())
        if not ids:
            return _empty_result()

        await session.execute(
            update(Send)
            .where(Send.id.in_(ids))
            .values(status="sending")
        )
        await session.commit()

    # Phase 2: reload the claimed batch + related rows, process each one in
    # its own transaction so a mid-batch failure doesn't roll back siblings.
    async with async_session_factory() as session:
        send_rows = list(
            (
                await session.execute(
                    select(Send).where(Send.id.in_(ids))
                )
            )
            .scalars()
            .all()
        )

        # Bulk-load templates and recipients referenced by this batch.
        template_ids = {s.template_id for s in send_rows}
        recipient_ids = {s.recipient_id for s in send_rows}
        tmpl_rows = (
            await session.execute(select(Template).where(Template.id.in_(template_ids)))
        ).scalars().all()
        rcp_rows = (
            await session.execute(select(Recipient).where(Recipient.id.in_(recipient_ids)))
        ).scalars().all()
        templates_by_id = {t.id: t for t in tmpl_rows}
        recipients_by_id = {r.id: r for r in rcp_rows}

        # Load every sending identity referenced by this batch once (grouped
        # by id, SPEC_ADDENDUM_003 §4) rather than per-send. Sends with a
        # NULL sending_identity_id (pre-addendum rows, or callers that never
        # resolved one) fall back to the bare/legacy adapter via
        # `get_esp_adapter(None)`.
        identity_ids = {
            s.sending_identity_id for s in send_rows if s.sending_identity_id is not None
        }
        identities_by_id: dict[uuid.UUID, SendingIdentity] = {}
        if identity_ids:
            identity_rows = (
                await session.execute(
                    select(SendingIdentity).where(SendingIdentity.id.in_(identity_ids))
                )
            ).scalars().all()
            identities_by_id = {i.id: i for i in identity_rows}

        # (a) Engagement-gap gate: subsumes the old flat 24h throttle per
        # SPEC_ADDENDUM_002 §2.2. Gap is max(24h, tier gap); dormant rows are
        # pushed far out here rather than gated on time — the nightly scorer
        # worker owns suppressing them. One query for the whole batch (not
        # per-recipient) — most recent prior send per claimed recipient,
        # gated against the tier map in-memory below.
        last_sent_rows = (
            await session.execute(
                select(Send.recipient_id, func.max(Send.sent_at))
                .where(
                    Send.recipient_id.in_(recipient_ids),
                    Send.sent_at.is_not(None),
                )
                .group_by(Send.recipient_id)
            )
        ).all()
        last_sent_by_recipient: dict[uuid.UUID, datetime] = dict(last_sent_rows)

        # (b) Weekly cap: count + earliest send in the rolling 7-day window
        # per claimed recipient, one batched query. Over-cap sends requeue to
        # the moment the oldest send in the window ages out (frees a slot).
        weekly_cutoff = now - WEEKLY_CAP_WINDOW
        weekly_rows = (
            await session.execute(
                select(Send.recipient_id, func.count(), func.min(Send.sent_at))
                .where(
                    Send.recipient_id.in_(recipient_ids),
                    Send.sent_at.is_not(None),
                    Send.sent_at >= weekly_cutoff,
                )
                .group_by(Send.recipient_id)
            )
        ).all()
        weekly_by_recipient: dict[uuid.UUID, tuple[int, datetime]] = {
            recipient_id: (count, oldest_sent_at)
            for recipient_id, count, oldest_sent_at in weekly_rows
        }

        # (c) Monthly cap: same shape as the weekly gate, 30-day window.
        monthly_cutoff = now - MONTHLY_CAP_WINDOW
        monthly_rows = (
            await session.execute(
                select(Send.recipient_id, func.count(), func.min(Send.sent_at))
                .where(
                    Send.recipient_id.in_(recipient_ids),
                    Send.sent_at.is_not(None),
                    Send.sent_at >= monthly_cutoff,
                )
                .group_by(Send.recipient_id)
            )
        ).all()
        monthly_by_recipient: dict[uuid.UUID, tuple[int, datetime]] = {
            recipient_id: (count, oldest_sent_at)
            for recipient_id, count, oldest_sent_at in monthly_rows
        }

        for send in send_rows:
            processed += 1
            recipient = recipients_by_id.get(send.recipient_id)

            if recipient is not None:
                # (a) Engagement-gap.
                tier = recipient.engagement_tier
                last_sent_at = last_sent_by_recipient.get(send.recipient_id)

                if tier == TIER_DORMANT:
                    next_eligible = (last_sent_at or now) + min_gap_for_tier(TIER_DORMANT)
                elif last_sent_at is not None:
                    gap = max(RECIPIENT_THROTTLE_WINDOW, min_gap_for_tier(tier))
                    next_eligible = last_sent_at + gap
                else:
                    next_eligible = None

                if next_eligible is not None and next_eligible > now:
                    # Leave it queued but push queued_at out to the moment it
                    # becomes eligible again, so the phase-1 claim query
                    # (which filters on queued_at <= now) doesn't
                    # immediately re-pick it and spin-loop on the same
                    # gated recipient.
                    send.status = "queued"
                    send.queued_at = next_eligible
                    throttled += 1
                    await session.commit()
                    log.info(
                        "send_worker.throttled",
                        send_id=str(send.id),
                        recipient_id=str(send.recipient_id),
                        tenant=send.tenant,
                        campaign_id=str(send.campaign_id),
                        tier=tier,
                        retry_at=next_eligible.isoformat(),
                    )
                    continue

                # (b) Weekly cap.
                weekly = weekly_by_recipient.get(send.recipient_id)
                if weekly is not None and weekly[0] >= settings.frequency_cap_per_week:
                    weekly_count, weekly_oldest = weekly
                    retry_at = weekly_oldest + WEEKLY_CAP_WINDOW
                    send.status = "queued"
                    send.queued_at = retry_at
                    weekly_capped += 1
                    await session.commit()
                    log.info(
                        "send_worker.weekly_capped",
                        send_id=str(send.id),
                        recipient_id=str(send.recipient_id),
                        tenant=send.tenant,
                        campaign_id=str(send.campaign_id),
                        count=weekly_count,
                        cap=settings.frequency_cap_per_week,
                        retry_at=retry_at.isoformat(),
                    )
                    continue

                # (c) Monthly cap.
                monthly = monthly_by_recipient.get(send.recipient_id)
                if monthly is not None and monthly[0] >= settings.frequency_cap_per_month:
                    monthly_count, monthly_oldest = monthly
                    retry_at = monthly_oldest + MONTHLY_CAP_WINDOW
                    send.status = "queued"
                    send.queued_at = retry_at
                    monthly_capped += 1
                    await session.commit()
                    log.info(
                        "send_worker.monthly_capped",
                        send_id=str(send.id),
                        recipient_id=str(send.recipient_id),
                        tenant=send.tenant,
                        campaign_id=str(send.campaign_id),
                        count=monthly_count,
                        cap=settings.frequency_cap_per_month,
                        retry_at=retry_at.isoformat(),
                    )
                    continue

                # (d) Quiet hours: recipient-local send window.
                state = (
                    recipient.attributes.get("state")
                    if isinstance(recipient.attributes, dict)
                    else None
                )
                zone_name = timezone_for_state(state) or settings.send_default_timezone
                window_open_at = next_window_open(
                    now_utc=now,
                    zone_name=zone_name,
                    start_hour=settings.send_window_start_hour,
                    end_hour=settings.send_window_end_hour,
                )
                if window_open_at > now:
                    send.status = "queued"
                    send.queued_at = window_open_at
                    quiet_hours_skipped += 1
                    await session.commit()
                    log.info(
                        "send_worker.quiet_hours_skipped",
                        send_id=str(send.id),
                        recipient_id=str(send.recipient_id),
                        tenant=send.tenant,
                        campaign_id=str(send.campaign_id),
                        zone=zone_name,
                        retry_at=window_open_at.isoformat(),
                    )
                    continue

            # (e) Per-day quota: stop dispatching once this cycle's budget
            # is spent. The initial batch claim already caps at
            # quota_remaining, so this is a defensive backstop rather than
            # the primary control — no extra query, just the in-memory
            # counter decremented on each successful send below.
            if quota_remaining <= 0:
                send.status = "queued"
                await session.commit()
                log.warning(
                    "send_worker.quota_exhausted_mid_cycle",
                    send_id=str(send.id),
                    recipient_id=str(send.recipient_id),
                    tenant=send.tenant,
                    campaign_id=str(send.campaign_id),
                )
                continue

            template = templates_by_id.get(send.template_id)

            if template is None or recipient is None:
                send.status = "failed"
                send.failure_reason = "missing template or recipient"
                failed += 1
                await session.commit()
                log.warning(
                    "send_worker.missing_refs",
                    send_id=str(send.id),
                    template_id=str(send.template_id),
                    recipient_id=str(send.recipient_id),
                )
                continue

            # (f) Suppression check at send time.
            hit = await session.execute(
                text(
                    "SELECT 1 FROM suppressions "
                    "WHERE tenant = :t AND email = :e LIMIT 1"
                ),
                {"t": send.tenant, "e": recipient.email},
            )
            if hit.first() is not None:
                send.status = "failed"
                send.failure_reason = "suppressed"
                suppressed += 1
                await session.commit()
                log.info(
                    "send_worker.suppressed",
                    send_id=str(send.id),
                    recipient_id=str(recipient.id),
                    tenant=send.tenant,
                    campaign_id=str(send.campaign_id),
                )
                continue

            # (g) Build unsub URL + render. The identity is resolved here
            # (not at dispatch) because its sending_domain decides the unsub
            # link host: unsub.<sending_domain>, settings fallback for
            # legacy NULL-identity sends.
            identity = identities_by_id.get(send.sending_identity_id)
            unsub_url = signer.build_url(
                UnsubPayload(
                    recipient_id=str(recipient.id),
                    send_id=str(send.id),
                    tenant=send.tenant,
                ),
                base_url=unsub_base_for_identity(identity),
            )
            try:
                subject, body_html, body_text = render_template(
                    template, recipient, {"unsub_url": unsub_url}
                )
            except MissingMergeTag as e:
                send.status = "failed"
                send.failure_reason = f"missing merge tag: {e}"[:500]
                failed += 1
                await session.commit()
                log.warning(
                    "send_worker.missing_merge_tag",
                    send_id=str(send.id),
                    recipient_id=str(recipient.id),
                    tenant=send.tenant,
                    campaign_id=str(send.campaign_id),
                    tag=str(e),
                )
                continue

            # (h) Inject unsub footer.
            body_html = _inject_unsub_html(body_html, unsub_url)
            body_text = _inject_unsub_text(body_text, unsub_url)

            # (i) Rate limit.
            await _take_token()

            # (j) Resolve this send's adapter and build the outbound
            # message. `from_address` comes from the identity when one is
            # stamped; the settings fallback only applies to legacy
            # (NULL sending_identity_id) sends.
            esp = get_esp_adapter(identity)
            from_address = (
                identity.from_address
                if identity is not None
                else settings.ses_from_address or "no-reply@example.com"
            )
            msg = OutboundMessage(
                to_email=recipient.email,
                from_address=from_address,
                subject=subject,
                body_html=body_html,
                body_text=body_text,
                configuration_set=settings.ses_configuration_set,
                list_unsubscribe=unsub_url,
                list_unsubscribe_post=True,
                message_tags={
                    "campaign_id": str(send.campaign_id),
                    "variant_label": send.variant_label,
                    "tenant": send.tenant,
                },
            )

            # (k) Dispatch.
            try:
                result = await esp.send(msg)
            except Exception as exc:  # noqa: BLE001
                send.status = "failed"
                send.failure_reason = str(exc)[:500]
                failed += 1
                await session.commit()
                log.exception(
                    "send_worker.send_failed",
                    send_id=str(send.id),
                    recipient_id=str(recipient.id),
                    tenant=send.tenant,
                    campaign_id=str(send.campaign_id),
                )
                continue

            # (l) Success.
            send.status = "sent"
            send.sent_at = datetime.now(timezone.utc)
            send.esp_message_id = result.esp_message_id
            send.esp = esp.name
            send.rendered_subject = subject
            sent += 1
            quota_remaining -= 1
            await session.commit()
            log.info(
                "send_worker.sent",
                send_id=str(send.id),
                recipient_id=str(recipient.id),
                tenant=send.tenant,
                campaign_id=str(send.campaign_id),
                esp=esp.name,
                esp_message_id=result.esp_message_id,
            )

    log.info(
        "send_worker.cycle_complete",
        processed=processed,
        sent=sent,
        suppressed=suppressed,
        failed=failed,
        throttled=throttled,
        weekly_capped=weekly_capped,
        monthly_capped=monthly_capped,
        quiet_hours_skipped=quiet_hours_skipped,
    )
    return {
        "processed": processed,
        "sent": sent,
        "suppressed": suppressed,
        "failed": failed,
        "throttled": throttled,
        "weekly_capped": weekly_capped,
        "monthly_capped": monthly_capped,
        "quiet_hours_skipped": quiet_hours_skipped,
    }
