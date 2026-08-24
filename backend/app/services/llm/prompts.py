"""LLM prompts — used VERBATIM from the project specification.

Do not paraphrase these; they are the exact strings required by the spec and
are stored per-summary in the database for auditability.
"""
from __future__ import annotations

# Verbatim from the spec.
PREVISIT_PROMPT_TEMPLATE = (
    "Analyse these symptoms and return: urgency level (Low / Medium / High), "
    "chief complaint, and three suggested questions for the doctor. "
    "Symptoms: {symptoms}"
)

# Verbatim from the spec.
POSTVISIT_PROMPT_TEMPLATE = (
    "Convert these clinical notes into a patient-friendly summary with "
    "medication schedule and follow-up steps: {notes}"
)


def previsit_prompt(symptoms: str) -> str:
    return PREVISIT_PROMPT_TEMPLATE.format(symptoms=symptoms)


def postvisit_prompt(notes: str) -> str:
    return POSTVISIT_PROMPT_TEMPLATE.format(notes=notes)
