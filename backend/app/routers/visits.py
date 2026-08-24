"""Doctor visit flow (JSON API): daily schedule with pre-visit summaries, and
visit completion → patient-friendly post-visit summary + prescriptions +
medication reminders.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..errors import BadRequest, Conflict, Forbidden, NotFound
from ..infra import idempotency, ratelimit
from ..models import (
    APPT_COMPLETED,
    APPT_CONFIRMED,
    APPT_HOLDING,
    ROLE_DOCTOR,
    SUMMARY_PREVISIT,
    Appointment,
    DoctorProfile,
    Prescription,
    Summary,
    User,
)
from ..schemas import CompleteVisitIn, PostVisitSummary, PreVisitSummary
from ..security import require_role, verify_csrf_header
from ..services import notifications, reminders
from ..services.llm.service import generate_postvisit_summary

router = APIRouter(prefix="/api/visits", tags=["visits"])


def _my_doctor(user: User) -> DoctorProfile:
    d = user.doctor_profile
    if d is None:
        raise Forbidden("No doctor profile for this account")
    return d


def _latest_previsit(db: Session, appointment_id: int) -> PreVisitSummary | None:
    row = db.scalars(
        select(Summary)
        .where(Summary.appointment_id == appointment_id, Summary.kind == SUMMARY_PREVISIT)
        .order_by(Summary.created_at.desc())
    ).first()
    if row is None or not row.data_json:
        return None
    try:
        return PreVisitSummary.model_validate_json(row.data_json)
    except Exception:  # noqa: BLE001
        return None


@router.get("/schedule")
def schedule(
    doctor_user: User = Depends(require_role(ROLE_DOCTOR)),
    db: Session = Depends(get_db),
):
    d = _my_doctor(doctor_user)
    appts = db.scalars(
        select(Appointment)
        .where(
            Appointment.doctor_id == d.id,
            Appointment.status.in_((APPT_HOLDING, APPT_CONFIRMED)),
        )
        .order_by(Appointment.scheduled_start)
    ).all()

    out = []
    for a in appts:
        previsit = _latest_previsit(db, a.id)
        out.append(
            {
                "appointment_id": a.id,
                "patient_name": a.patient.full_name,
                "status": a.status,
                "scheduled_start": a.scheduled_start.isoformat() if a.scheduled_start else None,
                "symptoms": a.symptoms,
                "urgency": previsit.urgency_level if previsit else None,
                "chief_complaint": previsit.chief_complaint if previsit else None,
                "suggested_questions": previsit.suggested_questions if previsit else [],
            }
        )
    return out


@router.get("/appointments/{appointment_id}/previsit")
def view_previsit(
    appointment_id: int,
    doctor_user: User = Depends(require_role(ROLE_DOCTOR)),
    db: Session = Depends(get_db),
):
    d = _my_doctor(doctor_user)
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise NotFound("Appointment not found")
    if appt.doctor_id != d.id:
        raise Forbidden("Not your appointment")
    previsit = _latest_previsit(db, appointment_id)
    return {
        "appointment_id": appt.id,
        "patient_name": appt.patient.full_name,
        "symptoms": appt.symptoms,
        "previsit": previsit.model_dump() if previsit else None,
    }


@router.post("/appointments/{appointment_id}/complete")
def complete_visit(
    appointment_id: int,
    payload: CompleteVisitIn,
    request: Request,
    _csrf: None = Depends(verify_csrf_header),
    doctor_user: User = Depends(require_role(ROLE_DOCTOR)),
    db: Session = Depends(get_db),
):
    if not ratelimit.allow("complete_visit", str(doctor_user.id), limit=20, window_seconds=300):
        raise BadRequest("Too many requests; please slow down")
    idem_key = request.headers.get("Idempotency-Key")
    cached = idempotency.get_cached("complete", idem_key, doctor_user.id) if idem_key else None
    if cached:
        return JSONResponse(cached["body"], status_code=cached["status_code"])
    d = _my_doctor(doctor_user)
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise NotFound("Appointment not found")
    if appt.doctor_id != d.id:
        raise Forbidden("Not your appointment")
    if appt.status == APPT_COMPLETED:
        # Idempotent: replay prior result without duplicating prescriptions
        existing = db.scalars(select(Summary).where(Summary.appointment_id == appt.id, Summary.kind == "postvisit").order_by(Summary.created_at.desc())).first()
        if existing and existing.data_json:
            try:
                pv = PostVisitSummary.model_validate_json(existing.data_json)
                body = {"appointment_id": appt.id, "status": appt.status, "postvisit": pv.model_dump(), "source": existing.status, "prescriptions": []}
                if idem_key:
                    idempotency.store("complete", idem_key, doctor_user.id, 200, body)
                return JSONResponse(body, status_code=200)
            except Exception:
                pass
        raise Conflict("This visit is already completed")
    if appt.status != APPT_CONFIRMED:
        raise Conflict("Only confirmed appointments can be completed")

    appt.doctor_notes = payload.doctor_notes
    appt.status = APPT_COMPLETED
    db.commit()
    db.refresh(appt)

    # Post-visit summary (LLM + deterministic fallback).
    summary = generate_postvisit_summary(db, appt, payload.doctor_notes)
    postvisit = PostVisitSummary.model_validate_json(summary.data_json)

    # Prescriptions + medication reminders.
    today = dt.datetime.now(ZoneInfo(settings.CLINIC_TZ)).date()
    created_prescriptions = []
    for p in payload.prescriptions:
        tpd_list, _is_prn = reminders.parse_frequency(p.frequency or "")
        # PRN stays 0 and generates no reminders; otherwise count dose times (at least 1)
        if tpd_list:
            times = len(tpd_list)
        else:
            times = 0 if _is_prn else 1
        presc = Prescription(
            appointment_id=appt.id,
            patient_id=appt.patient_id,
            doctor_id=d.id,
            medication_name=p.medication_name,
            dosage=p.dosage,
            frequency=p.frequency,
            times_per_day=times,
            duration_days=p.duration_days,
            instructions=p.instructions,
            start_date=today,
        )
        db.add(presc)
        db.commit()
        db.refresh(presc)
        n = reminders.generate_reminders(db, presc)
        created_prescriptions.append(
            {
                "id": presc.id,
                "medication_name": presc.medication_name,
                "dosage": presc.dosage,
                "frequency": presc.frequency,
                "duration_days": presc.duration_days,
                "reminders_scheduled": n,
            }
        )

    notifications.notify_postvisit_ready(appt, postvisit)

    body = {
        "appointment_id": appt.id,
        "status": appt.status,
        "postvisit": postvisit.model_dump(),
        "source": summary.status,  # "ok" (LLM) or "fallback"
        "prescriptions": created_prescriptions,
    }
    if idem_key:
        idempotency.store("complete", idem_key, doctor_user.id, 200, body)
    return JSONResponse(body, status_code=200)
