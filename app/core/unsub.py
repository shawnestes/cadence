from dataclasses import dataclass
from functools import lru_cache

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import get_settings


@dataclass(slots=True)
class UnsubPayload:
    recipient_id: str
    send_id: str | None
    tenant: str


_SALT = "cadence-unsub-v1"
_MAX_AGE_SECONDS = 60 * 60 * 24 * 180  # 180 days


class UnsubTokenError(Exception):
    pass


class UnsubSigner:
    def __init__(self, signing_key: str, base_url: str) -> None:
        self._serializer = URLSafeTimedSerializer(signing_key, salt=_SALT)
        self._base_url = base_url.rstrip("/")

    def sign(self, payload: UnsubPayload) -> str:
        return self._serializer.dumps(
            {"r": payload.recipient_id, "s": payload.send_id, "t": payload.tenant}
        )

    def verify(self, token: str, max_age: int = _MAX_AGE_SECONDS) -> UnsubPayload:
        try:
            data = self._serializer.loads(token, max_age=max_age)
        except SignatureExpired as e:
            raise UnsubTokenError("token expired") from e
        except BadSignature as e:
            raise UnsubTokenError("bad signature") from e
        return UnsubPayload(recipient_id=data["r"], send_id=data.get("s"), tenant=data["t"])

    def build_url(self, payload: UnsubPayload, base_url: str | None = None) -> str:
        base = base_url.rstrip("/") if base_url else self._base_url
        return f"{base}/u/{self.sign(payload)}"


def unsub_base_for_identity(identity) -> str | None:
    """Unsub link base for a sending identity: `unsub.<sending_domain>`,
    unless the identity's `provider_config.unsub_base_url` overrides it.
    None (also for `identity=None`) means the settings-level default."""
    if identity is None:
        return None
    config = identity.provider_config or {}
    return config.get("unsub_base_url") or f"https://unsub.{identity.sending_domain}"


@lru_cache(maxsize=1)
def get_unsub_signer() -> UnsubSigner:
    s = get_settings()
    return UnsubSigner(signing_key=s.unsub_signing_key, base_url=s.unsub_base_url)
