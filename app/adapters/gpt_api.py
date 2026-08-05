from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

log = structlog.get_logger(__name__)

# source code for provider_daily_rollup rows written by this adapter
_SOURCE_GPT_API: int = 2

# reputation_group smallint for gmail
_REPUTATION_GROUP_GMAIL: int = 1  # matches domain_provider seed value for gmail

# channel=1 is email
_CHANNEL_EMAIL: int = 1


async def fetch_and_store_gmail_complaint_rate(
    session: AsyncSession,
    tenant: str,
) -> None:
    """Fetch Gmail domain reputation from Google Postmaster Tools and write
    provider_daily_rollup rows with source=2.

    No-op when gpt_api_credentials_json is unset (D8). Missing data is logged
    as gpt_data_unavailable, never treated as a passing result.
    """
    settings = get_settings()
    if not settings.gpt_api_credentials_json:
        log.info(
            "gpt_api.gpt_data_unavailable",
            tenant=tenant,
            reason="credentials_not_configured",
        )
        return

    # TODO: implement real GPT API call using gpt_api_credentials_json.
    # Until then, log that data is unavailable even when creds exist,
    # so monitoring surfaces the gap rather than silently passing.
    log.info(
        "gpt_api.gpt_data_unavailable",
        tenant=tenant,
        reason="stub_not_implemented",
    )
