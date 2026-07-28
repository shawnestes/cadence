"""ESP adapter factory (SPEC_ADDENDUM_003 §3).

Routes a send to the adapter for its sending identity. `identity=None` keeps
the legacy, settings-based dispatch path alive for call sites that haven't
been migrated to identity-aware routing yet.
"""

from __future__ import annotations

import uuid

import structlog

from app.adapters.esp.base import ESPAdapter
from app.adapters.esp.noop import NoopESP
from app.adapters.esp.ses import SESAdapter
from app.adapters.esp.ses_smtp import SESSMTPAdapter
from app.core.config import get_settings
from app.models.sending_identity import SendingIdentity

logger = structlog.get_logger(__name__)

# Plain dicts, not `lru_cache`: `SendingIdentity` is a mutable SQLAlchemy ORM
# object (unhashable in the way `lru_cache` needs), so we cache by identity
# id instead of by the object itself.
_legacy_adapter: ESPAdapter | None = None
_adapters_by_identity_id: dict[uuid.UUID, ESPAdapter] = {}


def get_esp_adapter(identity: SendingIdentity | None = None) -> ESPAdapter:
    """Resolve the `ESPAdapter` to dispatch a send through.

    `CADENCE_ESP_BACKEND=noop` forces `NoopESP` unconditionally, regardless
    of `identity` -- this keeps dev/test environments unchanged. Otherwise:

    - `identity=None`: the legacy settings-based `SESAdapter` singleton, for
      call sites that predate per-identity routing.
    - `identity` given: an adapter built from the identity's own
      `esp_provider`/`provider_config`, cached per `identity.id`. An
      `esp_provider` this factory doesn't implement yet (SPEC_ADDENDUM_003
      §9: SendGrid/ExpertSender land later) degrades to `NoopESP` with a
      warning rather than raising, per the addendum's capability-gap rule.
    """
    global _legacy_adapter

    settings = get_settings()
    if settings.esp_backend != "ses":
        return NoopESP()

    if identity is None:
        if _legacy_adapter is None:
            _legacy_adapter = SESAdapter(settings)
        return _legacy_adapter

    cached = _adapters_by_identity_id.get(identity.id)
    if cached is not None:
        return cached

    if identity.esp_provider == "ses":
        adapter: ESPAdapter = SESAdapter(settings, identity=identity)
    elif identity.esp_provider == "ses_smtp":
        adapter = SESSMTPAdapter(settings, identity=identity)
    else:
        logger.warning(
            "esp_factory.unsupported_provider",
            identity_id=str(identity.id),
            esp_provider=identity.esp_provider,
        )
        adapter = NoopESP()

    _adapters_by_identity_id[identity.id] = adapter
    return adapter
