from datetime import datetime, timedelta, timezone
from html import escape

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.unsub import UnsubTokenError, get_unsub_signer
from app.models.recipient import Recipient
from app.models.suppression import Suppression
from app.models.unsub_outbox import UnsubOutbox

router = APIRouter(tags=["unsub"])
logger = structlog.get_logger(__name__)


_PAGE_STYLE = (
    "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
    "Arial,sans-serif;background:#f7f7f8;color:#1a1a1a;margin:0;padding:40px 20px;}"
    ".card{max-width:480px;margin:40px auto;background:#fff;padding:32px;border-radius:8px;"
    "box-shadow:0 1px 3px rgba(0,0,0,0.08);}"
    "h1{font-size:20px;margin:0 0 16px;}"
    "p{line-height:1.5;margin:0 0 20px;color:#444;}"
    "button{background:#1a1a1a;color:#fff;border:0;padding:12px 24px;border-radius:6px;"
    "font-size:15px;cursor:pointer;}"
    "button:hover{background:#333;}"
    ".muted{color:#777;font-size:13px;}"
)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>"
        f"<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{escape(title)}</title>"
        f"<style>{_PAGE_STYLE}</style></head>"
        f"<body><div class=\"card\">{body}</div></body></html>"
    )


@router.get("/u/{token}", response_class=HTMLResponse)
async def unsub_confirm(token: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    signer = get_unsub_signer()
    try:
        payload = signer.verify(token)
    except UnsubTokenError:
        raise HTTPException(
            status_code=410,
            detail="This unsubscribe link has expired or is invalid.",
        )

    result = await db.execute(
        select(Recipient).where(
            Recipient.tenant == payload.tenant,
            Recipient.id == payload.recipient_id,
        )
    )
    recipient = result.scalar_one_or_none()
    if recipient is None:
        raise HTTPException(status_code=410, detail="This unsubscribe link is no longer valid.")

    email = escape(recipient.email)
    tenant = escape(payload.tenant)
    action = f"/u/{escape(token)}"
    body = (
        f"<h1>Unsubscribe</h1>"
        f"<p>You're about to unsubscribe <strong>{email}</strong> from {tenant} emails. "
        f"Click below to confirm.</p>"
        f"<form method=\"POST\" action=\"{action}\">"
        f"<button type=\"submit\">Unsubscribe</button></form>"
    )
    return HTMLResponse(_page("Unsubscribe", body))


@router.post("/u/{token}")
async def unsub_one_click(token: str, db: AsyncSession = Depends(get_db)) -> Response:
    signer = get_unsub_signer()
    try:
        payload = signer.verify(token)
    except UnsubTokenError:
        return Response(status_code=410)

    result = await db.execute(
        select(Recipient).where(
            Recipient.tenant == payload.tenant,
            Recipient.id == payload.recipient_id,
        )
    )
    recipient = result.scalar_one_or_none()
    if recipient is None:
        return Response(status_code=404)

    now = datetime.now(timezone.utc)

    # Insert suppression (idempotent on (tenant, email)).
    suppression = Suppression(
        tenant=payload.tenant,
        email=recipient.email,
        reason="unsubscribe",
        source="one_click",
    )
    db.add(suppression)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        # Re-fetch recipient in the fresh session state.
        result = await db.execute(
            select(Recipient).where(
                Recipient.tenant == payload.tenant,
                Recipient.id == payload.recipient_id,
            )
        )
        recipient = result.scalar_one_or_none()
        if recipient is None:
            return Response(status_code=404)

    # Guard unsub_outbox against spamming — only insert if no pending row in last hour.
    one_hour_ago = now - timedelta(hours=1)
    existing = await db.execute(
        select(UnsubOutbox.id).where(
            UnsubOutbox.tenant == payload.tenant,
            UnsubOutbox.email == recipient.email,
            UnsubOutbox.delivered_at.is_(None),
            UnsubOutbox.created_at >= one_hour_ago,
        ).limit(1)
    )
    if existing.scalar_one_or_none() is None:
        db.add(
            UnsubOutbox(
                tenant=payload.tenant,
                email=recipient.email,
                reason="unsubscribe",
                occurred_at=now,
            )
        )

    recipient.status = "suppressed"
    await db.commit()

    logger.info(
        "unsub.one_click",
        tenant=payload.tenant,
        recipient_id=str(recipient.id),
        send_id=payload.send_id,
    )

    tenant = escape(payload.tenant)
    body = (
        f"<h1>Unsubscribed</h1>"
        f"<p>You've been unsubscribed from {tenant}.</p>"
        f"<p class=\"muted\">It may take a short time for this to propagate to in-flight sends.</p>"
    )
    return HTMLResponse(_page("Unsubscribed", body))
