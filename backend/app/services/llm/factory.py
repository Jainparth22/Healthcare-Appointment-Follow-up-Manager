"""Provider factory — selects the configured LLM provider."""
from __future__ import annotations

from functools import lru_cache

from ...config import settings
from .base import LLMProvider


@lru_cache
def get_provider() -> LLMProvider:
    provider = settings.LLM_PROVIDER.lower()
    if provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider()
    from .gemini import GeminiProvider

    return GeminiProvider()
