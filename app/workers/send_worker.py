"""Send worker: pulls queued sends, renders, rate-limits, and dispatches via ESP.

Per SPEC §5 (rendering), §10 (compliance — suppression check at send time,
unsub link + List-Unsubscribe headers), §13 (per-second rate limit, per-day
account quota, per-recipient 24h throttle). Per SPEC_ADDENDUM.md §2.2, no
warmup/IP-selection logic belongs here — SES Managed Dedicated IPs handles
that.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select, text, update

from app.adapters.esp.base import OutboundMessage
from app.adapters.esp.factory import get_esp_adapter
from app.core.config import get_settings
from app.core.db import async_session_factory
from app.core.unsub import UnsubPayload, get_unsub_signer
from app.domain.rendering import MissingMergeTag, render_template
from app.models.recipient import Recipient
from app.models.send import Send
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


async def run_once() -> dict[str, int]:
    settings = get_settings()
    batch_size = max(settings.send_rate_limit_per_second, 1)

    processed = 0
    sent = 0
    suppressed = 0
    failed = 0
    throttled = 0

    esp = get_esp_adapter()
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
        return {
            "processed": 0,
            "sent": 0,
            "suppressed": 0,
            "failed": 0,
            "throttled": 0,
        }

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
            return {
                "processed": 0,
                "sent": 0,
                "suppressed": 0,
                "failed": 0,
                "throttled": 0,
            }

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

        # (a) Per-recipient throttle: max 1 send per recipient per rolling
        # 24h. One query for the whole batch (not per-recipient) — find the
        # most recent prior send for each claimed recipient within the
        # window, then gate on that map in-memory below.
        throttle_cutoff = now - RECIPIENT_THROTTLE_WINDOW
        throttle_rows = (
            await session.execute(
                select(Send.recipient_id, func.max(Send.sent_at))
                .where(
                    Send.recipient_id.in_(recipient_ids),
                    Send.sent_at.is_not(None),
                    Send.sent_at >= throttle_cutoff,
                )
                .group_by(Send.recipient_id)
            )
        ).all()
        next_eligible_by_recipient = {
            recipient_id: last_sent_at + RECIPIENT_THROTTLE_WINDOW
            for recipient_id, last_sent_at in throttle_rows
        }

        for send in send_rows:
            processed += 1

            next_eligible = next_eligible_by_recipient.get(send.recipient_id)
            if next_eligible is not None:
                # Leave it queued but push queued_at out to the moment it
                # becomes eligible again, so the phase-1 claim query (which
                # filters on queued_at <= now) doesn't immediately re-pick
                # it and spin-loop on the same throttled recipient.
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
                    retry_at=next_eligible.isoformat(),
                )
                continue

            # (b) Per-day quota: stop dispatching once this cycle's budget
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
            recipient = recipients_by_id.get(send.recipient_id)

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

            # (c) Suppression check at send time.
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

            # (d) Build unsub URL + render.
            unsub_url = signer.build_url(
                UnsubPayload(
                    recipient_id=str(recipient.id),
                    send_id=str(send.id),
                    tenant=send.tenant,
                )
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

            # (e) Inject unsub footer.
            body_html = _inject_unsub_html(body_html, unsub_url)
            body_text = _inject_unsub_text(body_text, unsub_url)

            # (f) Rate limit.
            await _take_token()

            # (g) Build outbound message.
            msg = OutboundMessage(
                to_email=recipient.email,
                from_address=settings.ses_from_address or "no-reply@example.com",
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

            # (h) Dispatch.
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

            # (i) Success.
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

    return {
        "processed": processed,
        "sent": sent,
        "suppressed": suppressed,
        "failed": failed,
        "throttled": throttled,
    }
