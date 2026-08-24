"""Orchestration: build prompts, call the provider (with timeout + fallback),
persist the ``Summary`` row. Booking/visit flows call these and never see an
exception from the LLM layer.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

from ...models import (
    SUMMARY_FALLBACK,
    SUMMARY_OK,
    SUMMARY_POSTVISIT,
    SUMMARY_PREVISIT,
    Appointment,
    Summary,
)
from ...config import settings
from ...schemas import PreVisitSummary
from .base import call_with_timeout, fallback_postvisit, fallback_previsit, parse_postvisit
from .factory import get_provider
from .prompts import postvisit_prompt, previsit_prompt

logger = logging.getLogger(__name__)


def generate_previsit_summary(db: Session, appointment: Appointment) -> Summary:
    """Structured pre-visit summary from the appointment's symptoms."""
    symptoms = appointment.symptoms or ""
    prompt = previsit_prompt(symptoms)
    provider = get_provider()
    status = SUMMARY_OK
    raw = None
    started = time.monotonic()
    try:
        if not provider.available():
            raise RuntimeError("LLM provider not configured")
        data: PreVisitSummary = call_with_timeout(
            lambda: provider.previsit(prompt), settings.LLM_TIMEOUT_SECONDS
        )
        raw = data.model_dump_json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pre-visit LLM failed (%s); using fallback", exc)
        data = fallback_previsit(symptoms)
        status = SUMMARY_FALLBACK
        raw = f"[fallback] {exc}"
    latency = int((time.monotonic() - started) * 1000)

    summary = Summary(
        appointment_id=appointment.id,
        kind=SUMMARY_PREVISIT,
        provider=provider.name,
        model=provider.model,
        prompt=prompt,
        raw_output=raw,
        data_json=data.model_dump_json(),
        status=status,
        latency_ms=latency,
    )
    db.add(summary)
    db.commit()
    db.refresh(summary)
    return summary


def generate_postvisit_summary(db: Session, appointment: Appointment, notes: str) -> Summary:
    """Patient-friendly summary from the doctor's clinical notes."""
    prompt = postvisit_prompt(notes)
    provider = get_provider()
    status = SUMMARY_OK
    started = time.monotonic()
    try:
        if not provider.available():
            raise RuntimeError("LLM provider not configured")
        text = call_with_timeout(lambda: provider.postvisit(prompt), settings.LLM_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("post-visit LLM failed (%s); using fallback", exc)
        text = fallback_postvisit(notes)
        status = SUMMARY_FALLBACK
    latency = int((time.monotonic() - started) * 1000)

    parsed = parse_postvisit(text)
    summary = Summary(
        appointment_id=appointment.id,
        kind=SUMMARY_POSTVISIT,
        provider=provider.name,
        model=provider.model,
        prompt=prompt,
        raw_output=text,
        data_json=parsed.model_dump_json(),
        status=status,
        latency_ms=latency,
    )
    db.add(summary)
    db.commit()
    db.refresh(summary)
    return summary
