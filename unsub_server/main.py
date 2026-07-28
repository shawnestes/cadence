"""Standalone unsubscribe server. Serves every unsub.<domain> host; the
signed token (not the Host header) identifies tenant + recipient.

Routes mirror app/api/unsub.py in cadence -- keep behavior in lockstep."""

from __future__ import annotations

import os
from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from unsub_server import db
from unsub_server.tokens import UnsubTokenError, verify

app = FastAPI(title="cadence-unsub", docs_url=None, redoc_url=None, openapi_url=None)

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


def _signing_key() -> str:
    return os.environ["CADENCE_UNSUB_SIGNING_KEY"]


@app.get("/health")
async def health() -> dict:
    await db.ping()
    return {"status": "ok"}


@app.get("/u/{token}", response_class=HTMLResponse)
async def unsub_confirm(token: str) -> HTMLResponse:
    try:
        payload = verify(token, signing_key=_signing_key())
    except UnsubTokenError:
        return HTMLResponse(
            _page("Link expired", "<h1>Link expired</h1><p>This unsubscribe link has expired or is invalid.</p>"),
            status_code=410,
        )

    recipient = await db.fetch_recipient(payload.tenant, payload.recipient_id)
    if recipient is None:
        return HTMLResponse(
            _page("Link expired", "<h1>Link expired</h1><p>This unsubscribe link is no longer valid.</p>"),
            status_code=410,
        )

    email = escape(recipient["email"])
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


@app.post("/u/{token}")
async def unsub_one_click(token: str) -> Response:
    try:
        payload = verify(token, signing_key=_signing_key())
    except UnsubTokenError:
        return Response(status_code=410)

    recipient = await db.fetch_recipient(payload.tenant, payload.recipient_id)
    if recipient is None:
        return Response(status_code=404)

    await db.record_unsub(payload.tenant, payload.recipient_id, recipient["email"])

    tenant = escape(payload.tenant)
    body = (
        f"<h1>Unsubscribed</h1>"
        f"<p>You've been unsubscribed from {tenant}.</p>"
        f"<p class=\"muted\">It may take a short time for this to propagate to in-flight sends.</p>"
    )
    return HTMLResponse(_page("Unsubscribed", body))
