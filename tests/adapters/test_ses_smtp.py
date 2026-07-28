"""Tests for app.adapters.esp.ses_smtp.SESSMTPAdapter.

SMTP-credential sending path for SES accounts we hold no API keys for
(SPEC_ADDENDUM_003: per-identity adapter routing). Credentials are resolved
from environment variables named in `provider_config` -- never stored in the
DB row itself.
"""

from __future__ import annotations

import uuid

import pytest

from app.adapters.esp import factory
from app.adapters.esp.base import OutboundMessage
from app.adapters.esp.ses_smtp import SESSMTPAdapter, SESSMTPConfigError
from app.core.config import get_settings
from app.models.sending_identity import SendingIdentity


def _identity(**overrides) -> SendingIdentity:
    defaults = dict(
        id=uuid.uuid4(),
        tenant="otto",
        name="smtp-identity",
        esp_provider="ses_smtp",
        from_address="quotes@karztests.io",
        sending_domain="karztests.io",
        provider_config={
            "smtp_host": "email-smtp.us-west-2.amazonaws.com",
            "smtp_username_env": "TEST_SMTP_USERNAME",
            "smtp_password_env": "TEST_SMTP_PASSWORD",
        },
        status="active",
        is_default=False,
    )
    defaults.update(overrides)
    return SendingIdentity(**defaults)


def _message(**overrides) -> OutboundMessage:
    defaults = dict(
        to_email="lead@example.com",
        from_address="Karz Insurance <quotes@karztests.io>",
        subject="Your quote",
        body_html="<p>hello</p>",
        body_text="hello",
        list_unsubscribe="https://unsub.example.com/u/abc",
    )
    defaults.update(overrides)
    return OutboundMessage(**defaults)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("TEST_SMTP_USERNAME", "AKIATESTUSER")
    monkeypatch.setenv("TEST_SMTP_PASSWORD", "shhh-secret")


class FakeSMTP:
    """Stands in for smtplib.SMTP; records the protocol exchange."""

    instances: list["FakeSMTP"] = []
    data_reply: bytes = b"Ok 010001999a2b3c4d-deadbeef-cafe"

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[tuple] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("quit",))

    def starttls(self):
        self.calls.append(("starttls",))
        return (220, b"Ready")

    def login(self, user, password):
        self.calls.append(("login", user, password))
        return (235, b"Authentication successful")

    def mail(self, sender, options=()):
        self.calls.append(("mail", sender))
        return (250, b"Ok")

    def rcpt(self, recip, options=()):
        self.calls.append(("rcpt", recip))
        return (250, b"Ok")

    def data(self, msg):
        self.calls.append(("data", msg))
        return (250, self.data_reply)


@pytest.fixture
def fake_smtp(monkeypatch):
    from app.adapters.esp import ses_smtp

    FakeSMTP.instances = []
    FakeSMTP.data_reply = b"Ok 010001999a2b3c4d-deadbeef-cafe"
    monkeypatch.setattr(ses_smtp.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


# ------------------------------------------------------------------ config
def test_config_resolved_from_identity_and_env(creds):
    adapter = SESSMTPAdapter(get_settings(), identity=_identity())

    assert adapter._host == "email-smtp.us-west-2.amazonaws.com"
    assert adapter._port == 587
    assert adapter._username == "AKIATESTUSER"
    assert adapter._password == "shhh-secret"


def test_host_resolvable_via_env(creds, monkeypatch):
    monkeypatch.setenv("TEST_SMTP_HOST", "smtp-env.example.com")
    identity = _identity(
        provider_config={
            "smtp_host_env": "TEST_SMTP_HOST",
            "smtp_username_env": "TEST_SMTP_USERNAME",
            "smtp_password_env": "TEST_SMTP_PASSWORD",
        }
    )

    adapter = SESSMTPAdapter(get_settings(), identity=identity)

    assert adapter._host == "smtp-env.example.com"


def test_explicit_port_respected(creds):
    identity = _identity(
        provider_config={
            "smtp_host": "h",
            "smtp_port": 2587,
            "smtp_username_env": "TEST_SMTP_USERNAME",
            "smtp_password_env": "TEST_SMTP_PASSWORD",
        }
    )

    adapter = SESSMTPAdapter(get_settings(), identity=identity)

    assert adapter._port == 2587


def test_missing_credentials_raise_config_error(monkeypatch):
    monkeypatch.delenv("TEST_SMTP_USERNAME", raising=False)
    monkeypatch.delenv("TEST_SMTP_PASSWORD", raising=False)
    adapter = SESSMTPAdapter(get_settings(), identity=_identity())

    with pytest.raises(SESSMTPConfigError):
        adapter._username  # noqa: B018


# -------------------------------------------------------------------- send

async def test_send_speaks_starttls_and_authenticates(creds, fake_smtp):
    adapter = SESSMTPAdapter(get_settings(), identity=_identity())

    await adapter.send(_message())

    (conn,) = fake_smtp.instances
    ops = [c[0] for c in conn.calls]
    assert ops == ["starttls", "login", "mail", "rcpt", "data", "quit"]
    assert ("login", "AKIATESTUSER", "shhh-secret") in conn.calls



async def test_send_returns_ses_message_id_from_data_reply(creds, fake_smtp):
    adapter = SESSMTPAdapter(get_settings(), identity=_identity())

    result = await adapter.send(_message())

    assert result.esp_message_id == "010001999a2b3c4d-deadbeef-cafe"



async def test_send_unparseable_reply_still_returns_an_id(creds, fake_smtp):
    fake_smtp.data_reply = b"queued"
    adapter = SESSMTPAdapter(get_settings(), identity=_identity())

    result = await adapter.send(_message())

    assert result.esp_message_id



async def test_send_builds_compliant_mime(creds, fake_smtp):
    adapter = SESSMTPAdapter(get_settings(), identity=_identity())

    await adapter.send(_message())

    (conn,) = fake_smtp.instances
    raw = next(c[1] for c in conn.calls if c[0] == "data")
    text = raw.decode() if isinstance(raw, bytes) else raw
    assert "List-Unsubscribe: <https://unsub.example.com/u/abc>" in text
    assert "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in text
    assert "Subject: Your quote" in text
    assert "multipart/alternative" in text



async def test_send_uses_envelope_from_identity_and_rcpt_from_message(creds, fake_smtp):
    adapter = SESSMTPAdapter(get_settings(), identity=_identity())

    await adapter.send(_message())

    (conn,) = fake_smtp.instances
    assert ("mail", "quotes@karztests.io") in conn.calls
    assert ("rcpt", "lead@example.com") in conn.calls


# ----------------------------------------------------------------- factory
def test_factory_routes_ses_smtp_provider(monkeypatch, creds):
    patched = get_settings().model_copy(update={"esp_backend": "ses"})
    monkeypatch.setattr(factory, "get_settings", lambda: patched)
    factory._adapters_by_identity_id.clear()

    adapter = factory.get_esp_adapter(_identity())

    assert isinstance(adapter, SESSMTPAdapter)
    factory._adapters_by_identity_id.clear()
