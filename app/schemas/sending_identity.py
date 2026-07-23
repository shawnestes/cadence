"""Pydantic schemas for /api/sending-identities (SPEC_ADDENDUM_003 §8)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

EspProvider = Literal["ses", "sendgrid", "expertsender", "noop"]
SendingIdentityStatus = Literal["active", "paused"]

# Substrings that flag a provider_config key as a likely secret. credentials_ref
# is the only sanctioned place for secret material; provider_config is for
# non-secret routing/config values only.
_SECRET_KEY_MARKERS = ("secret", "password", "api_key")


def _reject_secret_keys(provider_config: dict | None) -> dict | None:
    if provider_config is None:
        return None
    for key in provider_config:
        lowered = key.lower()
        if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
            raise ValueError(
                f"provider_config key {key!r} looks like a secret; use credentials_ref instead"
            )
    return provider_config


class SendingIdentityIn(BaseModel):
    tenant: str = "otto"
    name: str
    esp_provider: EspProvider
    from_address: str
    sending_domain: str
    credentials_ref: str | None = None
    provider_config: dict = {}
    daily_quota: int = 50000
    status: SendingIdentityStatus = "active"
    is_default: bool = False

    @field_validator("from_address")
    @classmethod
    def _from_address_has_at(cls, value: str) -> str:
        if "@" not in value:
            raise ValueError("from_address must contain '@'")
        return value

    @field_validator("provider_config")
    @classmethod
    def _provider_config_no_secrets(cls, value: dict) -> dict:
        _reject_secret_keys(value)
        return value


class SendingIdentityUpdate(BaseModel):
    name: str | None = None
    esp_provider: EspProvider | None = None
    from_address: str | None = None
    sending_domain: str | None = None
    credentials_ref: str | None = None
    provider_config: dict | None = None
    daily_quota: int | None = None
    status: SendingIdentityStatus | None = None
    is_default: bool | None = None

    @field_validator("from_address")
    @classmethod
    def _from_address_has_at(cls, value: str | None) -> str | None:
        if value is not None and "@" not in value:
            raise ValueError("from_address must contain '@'")
        return value

    @field_validator("provider_config")
    @classmethod
    def _provider_config_no_secrets(cls, value: dict | None) -> dict | None:
        return _reject_secret_keys(value)


class SendingIdentityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant: str
    name: str
    esp_provider: str
    from_address: str
    sending_domain: str
    credentials_ref: str | None
    provider_config: dict
    daily_quota: int
    status: str
    is_default: bool
    created_at: datetime
    updated_at: datetime


class SendingIdentityListOut(BaseModel):
    sending_identities: list[SendingIdentityOut]
    total: int
