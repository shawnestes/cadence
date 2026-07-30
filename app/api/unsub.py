import uuid
from datetime import datetime, timedelta, timezone
from html import escape

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.unsub import UnsubTokenError, get_unsub_signer
from app.models.recipient import Recipient
from app.models.suppression import Suppression
from app.models.unsub_diagnostic import UnsubDiagnostic
from app.models.unsub_outbox import UnsubOutbox

router = APIRouter(tags=["unsub"])
logger = structlog.get_logger(__name__)


# Keep _PAGE_STYLE and the page bodies in lockstep with unsub_server/main.py:
# recipients must see the same page whichever service serves it.
_PAGE_STYLE = (
    ":root{--ink:#26221c;--sub:#6b645a;--faint:#8a8378;--line:#e6e1d7;--ok:#3d7a52;}"
    "*{box-sizing:border-box;}"
    "body{margin:0;padding:48px 20px;color:var(--ink);"
    "font:15px/1.6 'Avenir Next','Segoe UI',Helvetica,Arial,sans-serif;"
    "background:linear-gradient(180deg,#f8f6f1 0%,#efece4 100%);min-height:100vh;}"
    ".card{max-width:420px;margin:8vh auto 0;background:#fffdfa;padding:40px 36px 32px;"
    "border:1px solid var(--line);border-radius:14px;"
    "box-shadow:0 24px 48px -24px rgba(38,34,28,.18);}"
    ".kicker{font-size:11px;letter-spacing:.16em;text-transform:uppercase;"
    "color:var(--faint);margin:0 0 14px;}"
    "h1{font-family:'Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif;"
    "font-size:26px;font-weight:600;letter-spacing:-.01em;margin:0 0 14px;}"
    "p{margin:0 0 22px;color:var(--sub);}"
    "p strong{color:var(--ink);font-weight:600;}"
    "button{display:block;width:100%;background:var(--ink);color:#fffdfa;border:0;"
    "padding:14px 24px;border-radius:9px;font:inherit;font-size:15px;font-weight:500;"
    "cursor:pointer;transition:transform .12s ease,box-shadow .12s ease;}"
    "button:hover{transform:translateY(-1px);box-shadow:0 10px 22px -12px rgba(38,34,28,.5);}"
    "button:active{transform:translateY(0);box-shadow:none;}"
    ".muted{color:var(--faint);font-size:13px;margin:18px 0 0;}"
    ".check{width:44px;height:44px;margin:0 0 18px;display:block;}"
    ".foot{max-width:420px;margin:18px auto 0;text-align:center;"
    "font-size:12px;color:var(--faint);}"
)

_CHECK_SVG = (
    "<svg class=\"check\" viewBox=\"0 0 44 44\" fill=\"none\" aria-hidden=\"true\">"
    "<circle cx=\"22\" cy=\"22\" r=\"20.5\" stroke=\"#3d7a52\" stroke-width=\"3\"/>"
    "<path d=\"M13.5 22.5l6 6 11-12\" stroke=\"#3d7a52\" stroke-width=\"3\" "
    "stroke-linecap=\"round\" stroke-linejoin=\"round\"/></svg>"
)


def _list_domain(request: Request) -> str | None:
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if host.startswith("unsub.") and "." in host[6:]:
        return host[6:]
    return None


def _list_phrase(request: Request) -> str:
    domain = _list_domain(request)
    return f"<strong>{escape(domain)}</strong>" if domain else "this mailing list"


def _page(title: str, body: str, request: Request) -> str:
    domain = _list_domain(request)
    foot = f"Secure unsubscribe · {escape(domain)}" if domain else "Secure unsubscribe"
    return (
        "<!doctype html>"
        f"<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{escape(title)}</title>"
        f"<style>{_PAGE_STYLE}</style></head>"
        f"<body><div class=\"card\">{body}</div>"
        f"<div class=\"foot\">{foot}</div></body></html>"
    )


def _inactive_page(request: Request) -> str:
    body = (
        "<p class=\"kicker\">Email preferences</p>"
        "<h1>This link isn't active</h1>"
        "<p>It may have expired or already been used. If you still receive "
        "unwanted email, use the unsubscribe link in the newest message &mdash; "
        "those links are always current.</p>"
    )
    return _page("Link not active", body, request)


def _done_page(request: Request) -> str:
    domain = _list_domain(request)
    subject = f"<strong>{escape(domain)}</strong>" if domain else "This mailing list"
    body = (
        f"{_CHECK_SVG}"
        "<h1>You're unsubscribed</h1>"
        f"<p>{subject} won't send any more email to this address.</p>"
        "<p class=\"muted\">Already-scheduled messages may take a short time to stop.</p>"
    )
    return _page("Unsubscribed", body, request)


async def _record_diagnostic(
    db: AsyncSession, tenant: str | None, recipient_id: str | None, token: str, host: str | None
) -> None:
    try:
        rid = uuid.UUID(recipient_id) if recipient_id else None
    except ValueError:
        rid = None  # raw value still recoverable from the token column
    db.add(
        UnsubDiagnostic(
            tenant=tenant,
            recipient_id=rid,
            reason="unknown_recipient",
            token=token[:1024],
            host=host,
        )
    )
    await db.commit()


@router.get("/u/{token}", response_class=HTMLResponse)
async def unsub_confirm(
    token: str, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    signer = get_unsub_signer()
    try:
        payload = signer.verify(token)
    except UnsubTokenError:
        return HTMLResponse(_inactive_page(request), status_code=410)

    result = await db.execute(
        select(Recipient).where(
            Recipient.tenant == payload.tenant,
            Recipient.id == payload.recipient_id,
        )
    )
    recipient = result.scalar_one_or_none()
    if recipient is None:
        await _record_diagnostic(
            db, payload.tenant, payload.recipient_id, token, request.headers.get("host")
        )
        return HTMLResponse(_inactive_page(request), status_code=410)

    email = escape(recipient.email)
    action = f"/u/{escape(token)}"
    body = (
        "<p class=\"kicker\">Email preferences</p>"
        "<h1>Unsubscribe</h1>"
        f"<p>You're unsubscribing <strong>{email}</strong> from "
        f"{_list_phrase(request)} emails.</p>"
        f"<form method=\"POST\" action=\"{action}\">"
        "<button type=\"submit\">Unsubscribe</button></form>"
        "<p class=\"muted\">This applies only to this email address.</p>"
    )
    return HTMLResponse(_page("Unsubscribe", body, request))


@router.post("/u/{token}")
async def unsub_one_click(
    token: str, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
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
        # Never a user-facing error: show the normal completion page and
        # record the anomaly for diagnosis.
        await _record_diagnostic(
            db, payload.tenant, payload.recipient_id, token, request.headers.get("host")
        )
        logger.warning(
            "unsub.unknown_recipient",
            tenant=payload.tenant,
            recipient_id=payload.recipient_id,
        )
        return HTMLResponse(_done_page(request))

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
            await _record_diagnostic(
                db, payload.tenant, payload.recipient_id, token, request.headers.get("host")
            )
            return HTMLResponse(_done_page(request))

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
    return HTMLResponse(_done_page(request))
