from functools import lru_cache

from app.adapters.ai.anthropic import AnthropicClient
from app.adapters.ai.base import AIClient
from app.adapters.ai.mock import MockAIClient
from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_ai_client() -> AIClient:
    s = get_settings()
    if s.ai_backend == "anthropic":
        return AnthropicClient(s)
    return MockAIClient()
