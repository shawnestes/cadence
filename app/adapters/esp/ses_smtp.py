"""SES SMTP-interface adapter (SPEC_ADDENDUM_003 §3).

Sends through the SES SMTP endpoint with SMTP credentials instead of the
sesv2 API. Used for sending identities whose SES account we hold no IAM
API keys for -- the domain owner hands us SMTP credentials only.

Credentials are resolved at send time from environment variables named in
`provider_config` (`smtp_username_env` / `smtp_password_env`), falling back
to the project `.env` file; secrets never live in the DB row. No SES API
access means no reputation metrics, no account-level suppression writes,
and no SNS webhook wiring for these identities.
"""

from __future__ import annotations

import asyncio
import re
import smtplib
import uuid
from email.message import EmailMessage
from email.utils import parseaddr

import structlog
from dotenv import dotenv_values

from app.adapters.esp.base import (
    ESPAdapter,
    OutboundMessage,
    ParsedEvent,
    ReputationSnapshot,
    SendResult,
)

logger = structlog.get_logger(__name__)

_SMTP_TIMEOUT_SECONDS = 30
# SES's DATA reply is "Ok <ses-message-id>"; the id is what SNS event
# payloads would join on.
_DATA_REPLY_ID_RE = re.compile(r"Ok\s+(\S+)", re.IGNORECASE)


class SESSMTPConfigError(Exception):
    """A required smtp_* provider_config key or env var is missing."""


class SESSMTPError(Exception):
    """The SMTP conversation failed."""


def _resolve_env(name: str) -> str | None:
    import os

    value = os.environ.get(name)
    if value:
        return value
    # The API process may not export .env (pydantic-settings reads it
    # privately), so fall back to the file for non-CADENCE_ vars.
    return dotenv_values(".env").get(name)


class SESSMTPAdapter(ESPAdapter):
    name = "ses_smtp"

    def __init__(self, settings, identity) -> None:
        self._settings = settings
        self._identity = identity

    @property
    def _provider_config(self) -> dict:
        return self._identity.provider_config or {}

    @property
    def _host(self) -> str:
        cfg = self._provider_config
        host = cfg.get("smtp_host")
        if not host and cfg.get("smtp_host_env"):
            host = _resolve_env(cfg["smtp_host_env"])
        if not host:
            raise SESSMTPConfigError(
                "provider_config needs smtp_host or smtp_host_env"
            )
        return host

    @property
    def _port(self) -> int:
        return int(self._provider_config.get("smtp_port") or 587)

    def _credential(self, key: str) -> str:
        env_name = self._provider_config.get(key)
        if not env_name:
            raise SESSMTPConfigError(f"provider_config needs {key}")
        value = _resolve_env(env_name)
        if not value:
            raise SESSMTPConfigError(f"env var {env_name} (from {key}) is not set")
        return value

    @property
    def _username(self) -> str:
        return self._credential("smtp_username_env")

    @property
    def _password(self) -> str:
        return self._credential("smtp_password_env")

    # ------------------------------------------------------------------ send
    async def send(self, message: OutboundMessage) -> SendResult:
        try:
            code, reply = await asyncio.to_thread(self._send_sync, message)
        except (smtplib.SMTPException, OSError) as exc:
            logger.error("ses_smtp_send_failed", to=message.to_email, error=str(exc))
            raise SESSMTPError(str(exc)) from exc

        reply_text = reply.decode(errors="replace") if isinstance(reply, bytes) else str(reply)
        match = _DATA_REPLY_ID_RE.search(reply_text)
        esp_message_id = match.group(1) if match else f"smtp-{uuid.uuid4().hex}"
        return SendResult(
            esp_message_id=esp_message_id,
            raw={"smtp_code": code, "smtp_reply": reply_text},
        )

    def _send_sync(self, message: OutboundMessage) -> tuple[int, bytes]:
        raw_bytes = self._build_raw_mime(message)
        envelope_from = parseaddr(self._identity.from_address)[1] or self._identity.from_address

        with smtplib.SMTP(self._host, self._port, timeout=_SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.starttls()
            smtp.login(self._username, self._password)
            smtp.mail(envelope_from)
            code, reply = smtp.rcpt(message.to_email)
            if code not in (250, 251):
                raise SESSMTPError(f"recipient refused: {code} {reply!r}")
            code, reply = smtp.data(raw_bytes)
            if code != 250:
                raise SESSMTPError(f"message refused: {code} {reply!r}")
            return code, reply

    def _build_raw_mime(self, message: OutboundMessage) -> bytes:
        # Mirrors SESAdapter._build_raw_mime so both send paths produce
        # identical, compliance-complete messages.
        msg = EmailMessage()
        msg["From"] = message.from_address
        msg["To"] = message.to_email
        msg["Subject"] = message.subject

        if message.list_unsubscribe:
            msg["List-Unsubscribe"] = f"<{message.list_unsubscribe}>"
            if message.list_unsubscribe_post:
                msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        msg.set_content(message.body_text or "")
        msg.add_alternative(message.body_html or "", subtype="html")
        return msg.as_bytes()

    # -------------------------------------------------------- get_reputation
    async def get_reputation(self) -> ReputationSnapshot:
        # SMTP credentials carry no API access; reputation is unobservable.
        return ReputationSnapshot(
            account_sending_enabled=True,
            bounce_rate_24h=None,
            complaint_rate_24h=None,
        )

    # --------------------------------------------------------------- webhook
    def parse_webhook(self, payload: dict, signature: str | None) -> list[ParsedEvent]:
        # No SNS wiring exists for SMTP-credential identities; their events
        # never arrive on our webhook. Nothing to parse.
        return []

    # ----------------------------------------------------------- suppression
    async def add_to_esp_suppression(self, email: str, reason: str) -> None:
        # Account-level suppression needs the sesv2 API. Cadence's own
        # suppression list still gates every send; log so the gap is visible.
        logger.warning(
            "ses_smtp_suppression_unavailable", email=email, reason=reason
        )
