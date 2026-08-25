"""Patient appointment flow (JSON API).

hold → symptoms (+ pre-visit LLM summary) → confirm → cancel / reschedule, plus
"my appointments". State transitions live in ``services.booking`` (the CAS
state machine); this layer adds auth, idempotency, CSRF and side-effect
enqueueing (email + calendar) after the transaction commits.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import BadRequest
from ..infra import idempotency, ratelimit
from ..models import (
    ROLE_PATIENT,
    SUMMARY_POSTVISIT,
    SUMMARY_PREVISIT,
    Appointment,
    Slot,
    Summary,
    User,
)
from ..schemas import AppointmentOut, HoldIn, PreVisitSummary, RescheduleIn, SymptomsIn
from ..security import require_role, verify_csrf_header
from ..services import booking, notifications
from ..services.llm.service import generate_previsit_summary

router = APIRouter(prefix="/api/appointments", tags=["appointments"])


def _appt_body(appt: Appointment) -> dict:
    return AppointmentOut.model_validate(appt).model_dump(mode="json")


def _latest_summary(db: Session, appointment_id: int, kind: str) -> dict | None:
    row = db.scalars(
        select(Summary)
        .where(Summary.appointment_id == appointment_id, Summary.kind == kind)
        .order_by(Summary.created_at.desc())
    ).first()
    if row is None or not row.data_json:
        return None
    try:
        return json.loads(row.data_json)
    except (ValueError, TypeError):
        return None


@router.post("/hold", response_model=AppointmentOut, status_code=201)
def hold(
    payload: HoldIn,
    request: Request,
    _csrf: None = Depends(verify_csrf_header),
    patient: User = Depends(require_role(ROLE_PATIENT)),
    db: Session = Depends(get_db),
):
    if not ratelimit.allow("hold", str(patient.id), limit=30, window_seconds=300):
        raise BadRequest("Too many booking attempts; please slow down")

    idem_key = request.headers.get("Idempotency-Key")
    cached = idempotency.get_cached("hold", idem_key, patient.id) if idem_key else None
    if cached:
        return JSONResponse(cached["body"], status_code=cached["status_code"])

    appt = booking.hold_slot(db, patient.id, payload.slot_id)
    body = _appt_body(appt)
    slot = db.get(Slot, payload.slot_id)
    from ..config import settings as _s
    if slot and slot.hold_expires_at:
        body["hold_expires_at"] = slot.hold_expires_at.isoformat()
    body["hold_minutes"] = _s.HOLD_MINUTES
    if idem_key:
        idempotency.store("hold", idem_key, patient.id, 201, body)
    return JSONResponse(body, status_code=201)


@router.post("/{appointment_id}/symptoms")
def submit_symptoms(
    appointment_id: int,
    payload: SymptomsIn,
    _csrf: None = Depends(verify_csrf_header),
    patient: User = Depends(require_role(ROLE_PATIENT)),
    db: Session = Depends(get_db),
):
    """Attach symptoms and generate the pre-visit summary (LLM + fallback)."""
    appt = booking.set_symptoms(db, patient.id, appointment_id, payload.symptoms)
    summary = generate_previsit_summary(db, appt)
    previsit = PreVisitSummary.model_validate_json(summary.data_json)
    return {
        "appointment": _appt_body(appt),
        "previsit": previsit.model_dump(),
        "source": summary.status,  # "ok" (LLM) or "fallback"
    }


@router.post("/{appointment_id}/confirm", response_model=AppointmentOut)
def confirm(
    appointment_id: int,
    request: Request,
    _csrf: None = Depends(verify_csrf_header),
    patient: User = Depends(require_role(ROLE_PATIENT)),
    db: Session = Depends(get_db),
):
    if not ratelimit.allow("confirm", str(patient.id), limit=30, window_seconds=300):
        raise BadRequest("Too many requests; please slow down")
    idem_key = request.headers.get("Idempotency-Key")
    cached = idempotency.get_cached("confirm", idem_key, patient.id) if idem_key else None
    if cached:
        return JSONResponse(cached["body"], status_code=cached["status_code"])

    appt = booking.confirm_appointment(db, patient.id, appointment_id)
    notifications.notify_booking_confirmed(appt)
    body = _appt_body(appt)
    if idem_key:
        idempotency.store("confirm", idem_key, patient.id, 200, body)
    return JSONResponse(body, status_code=200)


@router.post("/{appointment_id}/cancel", response_model=AppointmentOut)
def cancel(
    appointment_id: int,
    request: Request,
    _csrf: None = Depends(verify_csrf_header),
    patient: User = Depends(require_role(ROLE_PATIENT)),
    db: Session = Depends(get_db),
):
    if not ratelimit.allow("cancel", str(patient.id), limit=30, window_seconds=300):
        raise BadRequest("Too many requests; please slow down")
    idem_key = request.headers.get("Idempotency-Key")
    cached = idempotency.get_cached("cancel", idem_key, patient.id) if idem_key else None
    if cached:
        return JSONResponse(cached["body"], status_code=cached["status_code"])
    appt = booking.cancel_appointment(db, appointment_id, by_user_id=patient.id)
    notifications.notify_cancellation(appt)
    body = _appt_body(appt)
    if idem_key:
        idempotency.store("cancel", idem_key, patient.id, 200, body)
    return JSONResponse(body, status_code=200)


@router.post("/{appointment_id}/reschedule", response_model=AppointmentOut)
def reschedule(
    appointment_id: int,
    payload: RescheduleIn,
    request: Request,
    _csrf: None = Depends(verify_csrf_header),
    patient: User = Depends(require_role(ROLE_PATIENT)),
    db: Session = Depends(get_db),
):
    if not ratelimit.allow("reschedule", str(patient.id), limit=30, window_seconds=300):
        raise BadRequest("Too many requests; please slow down")
    idem_key = request.headers.get("Idempotency-Key")
    cached = idempotency.get_cached("reschedule", idem_key, patient.id) if idem_key else None
    if cached:
        return JSONResponse(cached["body"], status_code=cached["status_code"])
    appt = booking.reschedule_appointment(db, patient.id, appointment_id, payload.new_slot_id)
    notifications.notify_reschedule(appt)
    body = _appt_body(appt)
    if idem_key:
        idempotency.store("reschedule", idem_key, patient.id, 200, body)
    return JSONResponse(body, status_code=200)


@router.get("/notifications")
def my_notifications(
    patient: User = Depends(require_role(ROLE_PATIENT)),
    db: Session = Depends(get_db),
):
    """Everything the outbox has sent to this patient, newest first."""
    from ..models import EmailOutbox

    rows = db.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.to_email == patient.email)
        .order_by(EmailOutbox.created_at.desc())
        .limit(50)
    ).all()
    return [
        {
            "id": r.id,
            "subject": r.subject,
            "preview": (r.body_text or "")[:160],
            "kind": r.kind,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/mine")
def my_appointments(
    patient: User = Depends(require_role(ROLE_PATIENT)),
    db: Session = Depends(get_db),
):
    """Rich list for the patient portal: doctor, times, symptoms, and — when
    available — the pre/post-visit summaries and the prescription schedule."""
    appts = db.scalars(
        select(Appointment)
        .where(Appointment.patient_id == patient.id)
        .order_by(Appointment.created_at.desc())
    ).all()

    out = []
    for a in appts:
        # Expand medication reminders per prescription for UI display
        presc_out = []
        for p in a.prescriptions:
            reminders = sorted(p.reminders, key=lambda r: r.scheduled_at) if hasattr(p, "reminders") else []
            presc_out.append(
                {
                    "medication_name": p.medication_name,
                    "dosage": p.dosage,
                    "frequency": p.frequency,
                    "duration_days": p.duration_days,
                    "instructions": p.instructions,
                    "times_per_day": p.times_per_day,
                    "reminders": [
                        {"scheduled_at": r.scheduled_at.isoformat(), "status": r.status}
                        for r in reminders
                    ],
                }
            )
        out.append(
            {
                "id": a.id,
                "status": a.status,
                "symptoms": a.symptoms,
                "doctor_notes": a.doctor_notes,
                "scheduled_start": a.scheduled_start.isoformat() if a.scheduled_start else None,
                "created_at": a.created_at.isoformat(),
                "doctor_id": a.doctor_id,
                "doctor_name": a.doctor.user.full_name if a.doctor else None,
                "specialisation": a.doctor.specialisation if a.doctor else None,
                "previsit": _latest_summary(db, a.id, SUMMARY_PREVISIT),
                "postvisit": _latest_summary(db, a.id, SUMMARY_POSTVISIT),
                "prescriptions": presc_out,
            }
        )
    return out
