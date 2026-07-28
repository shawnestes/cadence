"""Verify-only copy of cadence's unsub token signer (app/core/unsub.py).

Deliberately duplicated so this service never imports cadence code; the
cross-check tests in tests/test_unsub_server_tokens.py keep the copies in
lockstep. Salt and payload keys MUST match cadence exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_SALT = "cadence-unsub-v1"
_MAX_AGE_SECONDS = 60 * 60 * 24 * 180  # 180 days


class UnsubTokenError(Exception):
    pass


@dataclass(slots=True)
class UnsubPayload:
    recipient_id: str
    send_id: str | None
    tenant: str


def verify(token: str, signing_key: str, max_age: int = _MAX_AGE_SECONDS) -> UnsubPayload:
    serializer = URLSafeTimedSerializer(signing_key, salt=_SALT)
    try:
        data = serializer.loads(token, max_age=max_age)
    except SignatureExpired as e:
        raise UnsubTokenError("token expired") from e
    except BadSignature as e:
        raise UnsubTokenError("bad signature") from e
    return UnsubPayload(recipient_id=data["r"], send_id=data.get("s"), tenant=data["t"])
