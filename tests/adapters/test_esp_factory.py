"""Tests for app.adapters.esp.factory.get_esp_adapter (SPEC_ADDENDUM_003 §3).

Pure unit tests against in-memory `SendingIdentity` objects -- no DB writes
needed since the factory only reads attributes off the object it's given.
"""

from __future__ import annotations

import uuid

import pytest

from app.adapters.esp import factory
from app.adapters.esp.noop import NoopESP
from app.adapters.esp.ses import SESAdapter
from app.core.config import get_settings
from app.models.sending_identity import SendingIdentity


def _identity(**overrides) -> SendingIdentity:
    defaults = dict(
        id=uuid.uuid4(),
        tenant="otto",
        name="test-identity",
        esp_provider="ses",
        from_address="test-identity@cadencetests.io",
        sending_domain="cadencetests.io",
        provider_config={},
        status="active",
        is_default=False,
    )
    defaults.update(overrides)
    return SendingIdentity(**defaults)


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """The factory caches adapters in module-level dicts; each test needs a
    clean slate so settings overrides from one test can't leak into the
    next via a stale cached adapter."""
    factory._legacy_adapter = None
    factory._adapters_by_identity_id.clear()
    yield
    factory._legacy_adapter = None
    factory._adapters_by_identity_id.clear()


def _settings_override(monkeypatch, **overrides):
    patched = get_settings().model_copy(update=overrides)
    monkeypatch.setattr(factory, "get_settings", lambda: patched)
    return patched


def test_noop_backend_ignores_identity(monkeypatch):
    _settings_override(monkeypatch, esp_backend="noop")
    identity = _identity()

    adapter = factory.get_esp_adapter(identity)

    assert isinstance(adapter, NoopESP)


def test_no_identity_returns_legacy_ses_adapter(monkeypatch):
    settings = _settings_override(monkeypatch, esp_backend="ses", ses_from_address="legacy@cadencetests.io")

    adapter = factory.get_esp_adapter()

    assert isinstance(adapter, SESAdapter)
    assert adapter._from_address == "legacy@cadencetests.io"
    assert adapter._settings is settings


def test_no_identity_call_returns_same_cached_instance(monkeypatch):
    _settings_override(monkeypatch, esp_backend="ses")

    first = factory.get_esp_adapter()
    second = factory.get_esp_adapter()

    assert first is second


def test_ses_identity_configures_adapter_from_identity(monkeypatch):
    _settings_override(monkeypatch, esp_backend="ses", aws_region="us-east-1")
    identity = _identity(
        from_address="sender@otto-identity.com",
        provider_config={
            "region": "us-west-2",
            "configuration_set": "cfg-otto",
            "managed_pool_name": "pool-otto",
        },
    )

    adapter = factory.get_esp_adapter(identity)

    assert isinstance(adapter, SESAdapter)
    assert adapter._from_address == "sender@otto-identity.com"
    assert adapter._region == "us-west-2"
    assert adapter._configuration_set == "cfg-otto"
    assert adapter._managed_pool_name == "pool-otto"


def test_ses_identity_falls_back_to_settings_for_missing_provider_config(monkeypatch):
    _settings_override(
        monkeypatch,
        esp_backend="ses",
        aws_region="us-east-1",
        ses_configuration_set="settings-cfg",
        ses_managed_pool_name="settings-pool",
    )
    identity = _identity(provider_config={})

    adapter = factory.get_esp_adapter(identity)

    assert adapter._region == "us-east-1"
    assert adapter._configuration_set == "settings-cfg"
    assert adapter._managed_pool_name == "settings-pool"


def test_unknown_provider_degrades_to_noop(monkeypatch):
    _settings_override(monkeypatch, esp_backend="ses")
    identity = _identity(esp_provider="sendgrid")

    adapter = factory.get_esp_adapter(identity)

    assert isinstance(adapter, NoopESP)


def test_adapter_cached_per_identity_id(monkeypatch):
    _settings_override(monkeypatch, esp_backend="ses")
    identity = _identity()

    first = factory.get_esp_adapter(identity)
    # A distinct Python object with the same id should still hit the cache.
    same_id_identity = _identity(id=identity.id, from_address="different@cadencetests.io")
    second = factory.get_esp_adapter(same_id_identity)

    assert first is second
    # The cached adapter reflects whichever identity built it first.
    assert first._from_address == identity.from_address


def test_different_identities_get_different_adapters(monkeypatch):
    _settings_override(monkeypatch, esp_backend="ses")
    identity_a = _identity(from_address="a@cadencetests.io")
    identity_b = _identity(from_address="b@cadencetests.io")

    adapter_a = factory.get_esp_adapter(identity_a)
    adapter_b = factory.get_esp_adapter(identity_b)

    assert adapter_a is not adapter_b
    assert adapter_a._from_address == "a@cadencetests.io"
    assert adapter_b._from_address == "b@cadencetests.io"
