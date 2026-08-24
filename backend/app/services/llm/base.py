"""Provider protocol, timeout wrapper and the deterministic fallback."""
from __future__ import annotations

import concurrent.futures
import re
from typing import Protocol

from ...schemas import PostVisitSummary, PreVisitSummary


class LLMProvider(Protocol):
    name: str
    model: str

    def available(self) -> bool:
        ...

    def previsit(self, prompt: str) -> PreVisitSummary:
        """Return a structured pre-visit summary for the given prompt. May raise."""
        ...

    def postvisit(self, prompt: str) -> str:
        """Return patient-friendly summary text for the given prompt. May raise."""
        ...


def call_with_timeout(fn, timeout: float):
    """Run a blocking provider call with a hard timeout (raises TimeoutError)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn)
        return future.result(timeout=timeout)


# --------------------------------------------------------------------------
# Deterministic fallback (no external calls) — keeps the app fully functional
# --------------------------------------------------------------------------
_HIGH_KEYWORDS = (
    "chest pain", "shortness of breath", "difficulty breathing", "can't breathe",
    "cannot breathe", "unconscious", "severe bleeding", "stroke", "numbness",
    "slurred speech", "seizure", "fainting", "fainted", "suicidal", "suicide",
    "severe", "crushing", "paralysis", "anaphylaxis", "overdose", "blue lips",
)
_MEDIUM_KEYWORDS = (
    "fever", "high temperature", "persistent", "vomiting", "infection", "injury",
    "swelling", "rash", "dizzy", "dizziness", "migraine", "abdominal pain",
    "diarrhea", "diarrhoea", "dehydration", "pain", "cough", "sprain",
)


def _heuristic_urgency(symptoms: str) -> str:
    text = symptoms.lower()
    if any(k in text for k in _HIGH_KEYWORDS):
        return "High"
    if any(k in text for k in _MEDIUM_KEYWORDS):
        return "Medium"
    return "Low"


def _chief_complaint(symptoms: str) -> str:
    first = re.split(r"[.\n]", symptoms.strip(), maxsplit=1)[0].strip()
    if not first:
        return "General consultation"
    return (first[:117] + "...") if len(first) > 120 else first


def fallback_previsit(symptoms: str) -> PreVisitSummary:
    urgency = _heuristic_urgency(symptoms)
    return PreVisitSummary(
        urgency_level=urgency,
        chief_complaint=_chief_complaint(symptoms),
        suggested_questions=[
            "What is the most likely cause of my symptoms?",
            "What tests or next steps do you recommend?",
            "Are there warning signs that mean I should seek urgent care?",
        ],
    )


def fallback_postvisit(notes: str) -> str:
    trimmed = notes.strip()
    return (
        "Here is a plain-language summary of your visit:\n\n"
        f"{trimmed}\n\n"
        "Please follow the medication schedule shown below and complete the "
        "full course as directed. Book a follow-up if your symptoms persist or "
        "worsen, and seek urgent care for any severe or sudden changes."
    )


def parse_postvisit(text: str) -> PostVisitSummary:
    """Best-effort split of a free-text post-visit summary into sections."""
    meds: list[str] = []
    steps: list[str] = []
    for line in text.splitlines():
        low = line.lower()
        stripped = line.strip("-• \t")
        if not stripped:
            continue
        if any(w in low for w in ("mg", "tablet", "dose", "capsule", "daily", "hours", "medication")):
            meds.append(stripped)
        elif any(w in low for w in ("follow", "review", "return", "revisit", "next", "monitor")):
            steps.append(stripped)
    return PostVisitSummary(summary_text=text, medication_schedule=meds, follow_up_steps=steps)
