"""Per-identity unsubscribe hosts: links live on unsub.<sending_domain>.

Signed tokens are host-independent, so a per-identity base URL changes only
the link's host, never its verifiability.
"""

from __future__ import annotations

import uuid

from app.core.unsub import UnsubPayload, UnsubSigner, unsub_base_for_identity
from app.models.sending_identity import SendingIdentity


def _identity(**overrides) -> SendingIdentity:
    defaults = dict(
        id=uuid.uuid4(),
        tenant="otto",
        name="unsub-test",
        esp_provider="ses",
        from_address="quotes@karztests.io",
        sending_domain="karztests.io",
        provider_config={},
        status="active",
        is_default=False,
    )
    defaults.update(overrides)
    return SendingIdentity(**defaults)


def _payload() -> UnsubPayload:
    return UnsubPayload(recipient_id=str(uuid.uuid4()), send_id=None, tenant="otto")


def test_build_url_base_override_changes_host_only():
    signer = UnsubSigner(signing_key="k", base_url="http://default.example")

    url = signer.build_url(_payload(), base_url="https://unsub.karztests.io/")

    assert url.startswith("https://unsub.karztests.io/u/")


def test_build_url_override_token_still_verifies():
    signer = UnsubSigner(signing_key="k", base_url="http://default.example")
    payload = _payload()

    url = signer.build_url(payload, base_url="https://unsub.karztests.io")
    token = url.rsplit("/u/", 1)[1]

    assert signer.verify(token).recipient_id == payload.recipient_id


def test_build_url_without_override_keeps_default_base():
    signer = UnsubSigner(signing_key="k", base_url="http://default.example")

    assert signer.build_url(_payload()).startswith("http://default.example/u/")


def test_base_derived_from_sending_domain():
    assert unsub_base_for_identity(_identity()) == "https://unsub.karztests.io"


def test_provider_config_override_wins():
    identity = _identity(provider_config={"unsub_base_url": "https://u.other.io"})

    assert unsub_base_for_identity(identity) == "https://u.other.io"


def test_none_identity_yields_none():
    assert unsub_base_for_identity(None) is None
