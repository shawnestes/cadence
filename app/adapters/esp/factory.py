from functools import lru_cache

from app.adapters.esp.base import ESPAdapter
from app.adapters.esp.noop import NoopESP
from app.adapters.esp.ses import SESAdapter
from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_esp_adapter() -> ESPAdapter:
    s = get_settings()
    if s.esp_backend == "ses":
        return SESAdapter(s)
    return NoopESP()
