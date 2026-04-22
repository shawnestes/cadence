"""Send worker: pulls queued sends, renders, rate-limits, and dispatches via ESP.

Per SPEC §5 (rendering), §10 (compliance — suppression check at send time,
unsub link + List-Unsubscribe headers), §13 (per-second rate limit).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, text

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
    """Block until one token is available, then consume it."""
    global _tokens, _last_refill
    async with _lock:
        while True:
            rate = max(get_settings().send_rate_limit_per_second, 1)
            now = time.monotonic()
            elapsed = now - _last_refill
            _tokens = min(float(rate), _tokens + elapsed * rate)
            _last_refill = now
            if _tokens >= 1:
                _tokens -= 1
                return
            await asyncio.sleep(1.0 / rate)


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

    esp = get_esp_adapter()
    signer = get_unsub_signer()

    async with async_session_factory() as session:
        # Send has FK columns but no ORM relationships defined on the model,
        # so we load templates + recipients in bulk after locking the batch.
        stmt = (
            select(Send)
            .where(Send.status == "queued")
            .order_by(Send.queued_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        send_rows = list(result.scalars().all())

        if not send_rows:
            return {"processed": 0, "sent": 0, "suppressed": 0, "failed": 0}

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

        for send in send_rows:
            processed += 1
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

            # (a) Suppression check at send time.
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

            # (b) Build unsub URL + render.
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

            # (d) Inject unsub footer.
            body_html = _inject_unsub_html(body_html, unsub_url)
            body_text = _inject_unsub_text(body_text, unsub_url)

            # (e) Rate limit.
            await _take_token()

            # (f) Build outbound message.
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

            # (g) Dispatch.
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

            # (h) Success.
            send.status = "sent"
            send.sent_at = datetime.now(timezone.utc)
            send.esp_message_id = result.esp_message_id
            send.esp = esp.name
            send.rendered_subject = subject
            sent += 1
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
    }
