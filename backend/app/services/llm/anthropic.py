"""Optional Anthropic (Claude) provider — set LLM_PROVIDER=anthropic to use.

Pre-visit uses structured outputs via ``messages.parse``; post-visit is text.
"""
from __future__ import annotations

from ...config import settings
from ...schemas import PreVisitSummary


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        self.model = settings.ANTHROPIC_MODEL
        self._client = None

    def available(self) -> bool:
        return bool(settings.ANTHROPIC_API_KEY)

    def _client_or_raise(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        return self._client

    def previsit(self, prompt: str) -> PreVisitSummary:
        client = self._client_or_raise()
        resp = client.messages.parse(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            output_format=PreVisitSummary,
        )
        return resp.parsed_output

    def postvisit(self, prompt: str) -> str:
        client = self._client_or_raise()
        resp = client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        if not text:
            raise RuntimeError("Empty response from Anthropic")
        return text
