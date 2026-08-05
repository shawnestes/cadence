"""Unit tests for app.adapters.gpt_api — no-op stub behavior (D8).

Scenarios covered:
- test_noop_when_no_credentials: returns None with no error when credentials unset.
- test_noop_stub_when_credentials_set: returns None and logs gpt_data_unavailable
  even when credentials are provided (stub path, not yet implemented).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters import gpt_api

_TENANT = "otto"


@pytest.mark.asyncio
async def test_noop_when_no_credentials():
    """D8: fetch_and_store_gmail_complaint_rate is a no-op when credentials unset.

    monkeypatches get_settings() to return gpt_api_credentials_json=None.
    Asserts the function returns None without raising.
    """
    mock_session = AsyncMock()

    with patch("app.adapters.gpt_api.get_settings") as mock_get_settings:
        mock_settings = MagicMock()
        mock_settings.gpt_api_credentials_json = None
        mock_get_settings.return_value = mock_settings

        result = await gpt_api.fetch_and_store_gmail_complaint_rate(
            mock_session, _TENANT
        )

    # Function should return None (implicit) — no-op path
    assert result is None
    # Session must not have been touched (no DB writes for missing creds)
    mock_session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_noop_stub_when_credentials_set():
    """D8: fetch_and_store_gmail_complaint_rate is still a no-op stub even when
    credentials are configured. Returns None and logs gpt_data_unavailable.
    """
    mock_session = AsyncMock()

    with patch("app.adapters.gpt_api.get_settings") as mock_get_settings, \
         patch("app.adapters.gpt_api.log") as mock_log:
        mock_settings = MagicMock()
        mock_settings.gpt_api_credentials_json = '{"type": "service_account"}'
        mock_get_settings.return_value = mock_settings

        result = await gpt_api.fetch_and_store_gmail_complaint_rate(
            mock_session, _TENANT
        )

    # Still a no-op stub — returns None
    assert result is None

    # Must log gpt_data_unavailable (not silently pass)
    mock_log.info.assert_called_once()
    call_kwargs = mock_log.info.call_args
    # First positional arg is the event name
    event_name = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("event", "")
    assert "gpt_data_unavailable" in event_name, (
        f"Expected 'gpt_data_unavailable' log event, got: {event_name!r}"
    )
