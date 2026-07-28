"""Cross-check: tokens signed by cadence verify in unsub_server, and drift
in salt/payload shape breaks this file."""

import pytest

from app.core.unsub import UnsubPayload as CadencePayload, UnsubSigner
from unsub_server.tokens import UnsubPayload, UnsubTokenError, verify

KEY = "cross-check-key"


def _cadence_token(recipient_id="rid-1", send_id="sid-1", tenant="otto") -> str:
    signer = UnsubSigner(signing_key=KEY, base_url="http://x")
    return signer.sign(CadencePayload(recipient_id=recipient_id, send_id=send_id, tenant=tenant))


def test_cadence_signed_token_verifies_in_unsub_server():
    payload = verify(_cadence_token(), signing_key=KEY)
    assert payload == UnsubPayload(recipient_id="rid-1", send_id="sid-1", tenant="otto")


def test_null_send_id_roundtrips():
    token = _cadence_token(send_id=None)
    assert verify(token, signing_key=KEY).send_id is None


def test_wrong_key_rejected():
    with pytest.raises(UnsubTokenError):
        verify(_cadence_token(), signing_key="other-key")


def test_garbage_rejected():
    with pytest.raises(UnsubTokenError):
        verify("not-a-token", signing_key=KEY)


def test_expired_rejected():
    with pytest.raises(UnsubTokenError):
        verify(_cadence_token(), signing_key=KEY, max_age=-1)
