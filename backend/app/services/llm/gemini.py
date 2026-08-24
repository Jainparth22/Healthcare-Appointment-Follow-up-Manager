"""Google Gemini provider (default). Uses the `google-genai` SDK.

Pre-visit uses structured output (`response_schema`) so the model returns the
exact ``PreVisitSummary`` shape. Post-visit is free text.
"""
from __future__ import annotations

from ...config import settings
from ...schemas import PreVisitSummary


class GeminiProvider:
    name = "gemini"

    def __init__(self) -> None:
        self.model = settings.GEMINI_MODEL
        self._client = None

    def available(self) -> bool:
        return bool(settings.GEMINI_API_KEY)

    def _client_or_raise(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    def previsit(self, prompt: str) -> PreVisitSummary:
        from google.genai import types

        client = self._client_or_raise()
        resp = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PreVisitSummary,
                temperature=0.2,
            ),
        )
        # Prefer parsing the JSON text; fall back to the SDK's parsed object.
        try:
            return PreVisitSummary.model_validate_json(resp.text)
        except Exception:
            parsed = getattr(resp, "parsed", None)
            if isinstance(parsed, PreVisitSummary):
                return parsed
            if parsed is not None:
                return PreVisitSummary.model_validate(parsed)
            raise

    def postvisit(self, prompt: str) -> str:
        client = self._client_or_raise()
        resp = client.models.generate_content(model=self.model, contents=prompt)
        text = (resp.text or "").strip()
        if not text:
            raise RuntimeError("Empty response from Gemini")
        return text
