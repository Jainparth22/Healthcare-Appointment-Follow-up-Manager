"""(e) LLM graceful degradation — with no API key the pre-visit summary still
returns a valid, structured result via the deterministic fallback, is persisted
with status='fallback', and stores the verbatim PDF prompt."""
from __future__ import annotations

import pytest

from app.models import (
    APPT_HOLDING,
    SUMMARY_FALLBACK,
    SUMMARY_PREVISIT,
    Appointment,
)
from app.schemas import PreVisitSummary
from app.services.llm.service import generate_previsit_summary


def _make_appt(db, doctor, patient, symptoms):
    appt = Appointment(
        doctor_id=doctor.id,
        patient_id=patient.id,
        status=APPT_HOLDING,
        symptoms=symptoms,
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return appt


def test_previsit_fallback_is_valid_and_persisted(db, make_doctor, users):
    doctor = make_doctor(with_hours=False)
    patient = users()
    appt = _make_appt(db, doctor, patient, "crushing chest pain radiating to the left arm")

    summary = generate_previsit_summary(db, appt)

    assert summary.status == SUMMARY_FALLBACK  # no GEMINI_API_KEY → fallback
    assert summary.kind == SUMMARY_PREVISIT
    # Prompt stored verbatim per the PDF spec.
    assert "Analyse these symptoms" in summary.prompt
    assert "chest pain" in summary.prompt

    parsed = PreVisitSummary.model_validate_json(summary.data_json)
    assert parsed.urgency_level == "High"  # chest-pain heuristic
    assert 1 <= len(parsed.suggested_questions) <= 3
    assert parsed.chief_complaint


@pytest.mark.parametrize(
    "symptoms",
    ["mild runny nose since yesterday", "persistent high fever and body pain", "chest pain"],
)
def test_previsit_urgency_always_valid_literal(db, make_doctor, users, symptoms):
    doctor = make_doctor(with_hours=False)
    patient = users()
    appt = _make_appt(db, doctor, patient, symptoms)

    parsed = PreVisitSummary.model_validate_json(generate_previsit_summary(db, appt).data_json)
    assert parsed.urgency_level in ("Low", "Medium", "High")
