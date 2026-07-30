"""Standalone unsubscribe server. Serves every unsub.<domain> host; the
signed token (not the Host header) identifies tenant + recipient. The Host
header is used only as escaped display text naming the list.

Routes mirror app/api/unsub.py in cadence -- keep behavior and markup in
lockstep. Unknown-recipient lookups never surface as errors to the user;
they are recorded in unsub_diagnostics instead."""

from __future__ import annotations

import os
from html import escape

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from unsub_server import db
from unsub_server.tokens import UnsubTokenError, verify

app = FastAPI(title="cadence-unsub", docs_url=None, redoc_url=None, openapi_url=None)
logger = structlog.get_logger()

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
    """Display name for the list, derived from the Host header.

    `unsub.karzinsurance.com` -> `karzinsurance.com`. Returns None when the
    host doesn't identify a sending domain (raw IP, localhost, tests).
    """
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


def _signing_key() -> str:
    return os.environ["CADENCE_UNSUB_SIGNING_KEY"]


@app.get("/health")
async def health() -> dict:
    await db.ping()
    return {"status": "ok"}


@app.get("/u/{token}", response_class=HTMLResponse)
async def unsub_confirm(token: str, request: Request) -> HTMLResponse:
    try:
        payload = verify(token, signing_key=_signing_key())
    except UnsubTokenError:
        return HTMLResponse(_inactive_page(request), status_code=410)

    recipient = await db.fetch_recipient(payload.tenant, payload.recipient_id)
    if recipient is None:
        await db.insert_diagnostic(
            tenant=payload.tenant,
            recipient_id=payload.recipient_id,
            reason="unknown_recipient",
            token=token,
            host=request.headers.get("host"),
        )
        return HTMLResponse(_inactive_page(request), status_code=410)

    email = escape(recipient["email"])
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


@app.post("/u/{token}")
async def unsub_one_click(token: str, request: Request) -> Response:
    try:
        payload = verify(token, signing_key=_signing_key())
    except UnsubTokenError:
        return Response(status_code=410)

    recipient = await db.fetch_recipient(payload.tenant, payload.recipient_id)
    if recipient is None:
        # Never a user-facing error: show the normal completion page and
        # record the anomaly for diagnosis.
        await db.insert_diagnostic(
            tenant=payload.tenant,
            recipient_id=payload.recipient_id,
            reason="unknown_recipient",
            token=token,
            host=request.headers.get("host"),
        )
        logger.warning(
            "unsub.unknown_recipient",
            tenant=payload.tenant,
            recipient_id=payload.recipient_id,
        )
        return HTMLResponse(_done_page(request))

    await db.record_unsub(payload.tenant, payload.recipient_id, recipient["email"])
    logger.info(
        "unsub.one_click",
        tenant=payload.tenant,
        recipient_id=payload.recipient_id,
        send_id=payload.send_id,
    )
    return HTMLResponse(_done_page(request))
