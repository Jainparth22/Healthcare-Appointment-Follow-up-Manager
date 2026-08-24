"""Admin API (JSON): manage doctors, working hours, leave (with patient
notification), and inspect the email outbox / dead-letter queue.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import NotFound
from ..models import (
    ROLE_ADMIN,
    ROLE_DOCTOR,
    ROLE_PATIENT,
    Appointment,
    DoctorLeave,
    DoctorProfile,
    DoctorWorkingHours,
    EmailOutbox,
    Slot,
    User,
)
from ..schemas import (
    DoctorCreateIn,
    DoctorOut,
    DoctorUpdateIn,
    LeaveIn,
    WorkingHourIn,
)
from ..infra import idempotency, ratelimit
from ..infra.cache import bump_search_version
from ..security import require_role, verify_csrf_header
from ..services import booking, notifications
from ..services.accounts import register_user

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_role(ROLE_ADMIN))])


def _doctor_out(d: DoctorProfile) -> dict:
    return {
        "id": d.id,
        "full_name": d.user.full_name,
        "specialisation": d.specialisation,
        "bio": d.bio,
        "slot_duration_min": d.slot_duration_min,
        "active": d.active,
    }


def _apply_working_hours(db: Session, doctor: DoctorProfile, hours: list[WorkingHourIn]) -> None:
    db.execute(
        DoctorWorkingHours.__table__.delete().where(DoctorWorkingHours.doctor_id == doctor.id)
    )
    for wh in hours:
        db.add(
            DoctorWorkingHours(
                doctor_id=doctor.id,
                day_of_week=wh.day_of_week,
                start_time=wh.start_time,
                end_time=wh.end_time,
            )
        )
    db.commit()


# --------------------------------------------------------------------------
# Doctor CRUD
# --------------------------------------------------------------------------
@router.post("/doctors", response_model=DoctorOut, status_code=201)
def create_doctor(
    payload: DoctorCreateIn,
    _csrf: None = Depends(verify_csrf_header),
    db: Session = Depends(get_db),
):
    user = register_user(
        db,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        phone=payload.phone,
        role=ROLE_DOCTOR,
    )
    doctor = DoctorProfile(
        user_id=user.id,
        specialisation=payload.specialisation,
        bio=payload.bio,
        slot_duration_min=payload.slot_duration_min,
        active=True,
    )
    db.add(doctor)
    db.commit()
    db.refresh(doctor)
    if payload.working_hours:
        _apply_working_hours(db, doctor, payload.working_hours)
        booking.regenerate_future_free_slots(db, doctor.id)
    bump_search_version()
    return _doctor_out(doctor)


@router.get("/doctors", response_model=list[DoctorOut])
def list_doctors(db: Session = Depends(get_db)):
    doctors = db.scalars(select(DoctorProfile).join(User).order_by(DoctorProfile.specialisation)).all()
    return [_doctor_out(d) for d in doctors]


@router.get("/doctors/{doctor_id}")
def get_doctor(doctor_id: int, db: Session = Depends(get_db)):
    d = db.get(DoctorProfile, doctor_id)
    if d is None:
        raise NotFound("Doctor not found")
    hours = sorted(d.working_hours, key=lambda w: (w.day_of_week, w.start_time))
    leaves = sorted(d.leaves, key=lambda x: x.leave_date)
    return {
        **_doctor_out(d),
        "working_hours": [
            {"day_of_week": w.day_of_week, "start_time": w.start_time.isoformat(), "end_time": w.end_time.isoformat()}
            for w in hours
        ],
        "leaves": [{"leave_date": x.leave_date.isoformat(), "reason": x.reason} for x in leaves],
    }


@router.patch("/doctors/{doctor_id}", response_model=DoctorOut)
def update_doctor(
    doctor_id: int,
    payload: DoctorUpdateIn,
    _csrf: None = Depends(verify_csrf_header),
    db: Session = Depends(get_db),
):
    d = db.get(DoctorProfile, doctor_id)
    if d is None:
        raise NotFound("Doctor not found")
    duration_changed = False
    if payload.full_name is not None:
        d.user.full_name = payload.full_name
    if payload.specialisation is not None:
        d.specialisation = payload.specialisation
    if payload.bio is not None:
        d.bio = payload.bio
    if payload.slot_duration_min is not None and payload.slot_duration_min != d.slot_duration_min:
        d.slot_duration_min = payload.slot_duration_min
        duration_changed = True
    if payload.active is not None:
        d.active = payload.active
    db.commit()
    db.refresh(d)
    # Slot cadence changed → rebuild future free slots on the new grid.
    if duration_changed and d.active:
        booking.regenerate_future_free_slots(db, d.id)
    else:
        from ..infra.cache import bump_doctor_version

        bump_doctor_version(d.id)
    bump_search_version()
    return _doctor_out(d)


@router.put("/doctors/{doctor_id}/working-hours", response_model=list[WorkingHourIn])
def set_working_hours(
    doctor_id: int,
    hours: list[WorkingHourIn],
    _csrf: None = Depends(verify_csrf_header),
    db: Session = Depends(get_db),
):
    d = db.get(DoctorProfile, doctor_id)
    if d is None:
        raise NotFound("Doctor not found")
    _apply_working_hours(db, d, hours)
    booking.regenerate_future_free_slots(db, d.id)
    bump_search_version()
    return hours


# --------------------------------------------------------------------------
# Leave (with cascade cancellation + patient notification)
# --------------------------------------------------------------------------
@router.post("/doctors/{doctor_id}/leave")
def add_leave(
    doctor_id: int,
    payload: LeaveIn,
    request: Request,
    _csrf: None = Depends(verify_csrf_header),
    db: Session = Depends(get_db),
):
    # Ratelimit admin leave mutations
    try:
        from ..security import get_current_user
        user = get_current_user(request, db)  # already verified via router dep, but fetch for key
        uid = str(user.id)
    except Exception:
        uid = request.client.host if request.client else "unknown"
    if not ratelimit.allow("leave", uid, limit=20, window_seconds=300):
        from ..errors import BadRequest
        raise BadRequest("Too many requests; please slow down")
    idem_key = request.headers.get("Idempotency-Key")
    cached = idempotency.get_cached("leave", idem_key, int(uid) if uid.isdigit() else 0) if idem_key else None
    if cached:
        return JSONResponse(cached["body"], status_code=cached["status_code"])
    affected = booking.apply_leave(db, doctor_id, payload.leave_date, payload.reason)
    notifications.notify_leave_cancellations(affected, payload.leave_date)
    body = {
        "leave_date": payload.leave_date.isoformat(),
        "cancelled_appointments": len(affected),
        "patients_notified": [a.patient_email for a in affected],
    }
    if idem_key:
        idempotency.store("leave", idem_key, int(uid) if uid.isdigit() else 0, 200, body)
    return JSONResponse(body, status_code=200)


@router.delete("/doctors/{doctor_id}/leave/{leave_date}")
def remove_leave(
    doctor_id: int,
    leave_date: dt.date,
    _csrf: None = Depends(verify_csrf_header),
    db: Session = Depends(get_db),
):
    booking.remove_leave(db, doctor_id, leave_date)
    return {"detail": "leave removed", "leave_date": leave_date.isoformat()}


@router.get("/stats")
def get_stats(db: Session = Depends(get_db)):
    total_patients = db.scalar(select(func.count(User.id)).where(User.role == ROLE_PATIENT)) or 0
    total_doctors = db.scalar(select(func.count(DoctorProfile.id))) or 0
    total_appointments = db.scalar(select(func.count(Appointment.id))) or 0
    total_slots = db.scalar(select(func.count(Slot.id))) or 0
    by_status = {row[0]: row[1] for row in db.execute(select(Appointment.status, func.count(Appointment.id)).group_by(Appointment.status)).all()}
    outbox_by_status = {row[0]: row[1] for row in db.execute(select(EmailOutbox.status, func.count(EmailOutbox.id)).group_by(EmailOutbox.status)).all()}
    recent_appointments = [
        {
            "id": a.id,
            "patient": a.patient.full_name if a.patient else None,
            "doctor": a.doctor.user.full_name if a.doctor and a.doctor.user else None,
            "status": a.status,
            "scheduled_start": a.scheduled_start.isoformat() if a.scheduled_start else None,
        }
        for a in db.scalars(select(Appointment).order_by(Appointment.created_at.desc()).limit(5)).all()
    ]
    return {
        "totals": {
            "patients": total_patients,
            "doctors": total_doctors,
            "appointments": total_appointments,
            "slots": total_slots,
        },
        "appointments_by_status": by_status,
        "outbox_by_status": outbox_by_status,
        "recent_appointments": recent_appointments,
    }


@router.get("/patients")
def list_patients(limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).where(User.role == ROLE_PATIENT).order_by(User.created_at.desc()).limit(limit)).all()
    return [{"id": u.id, "full_name": u.full_name, "email": u.email, "phone": u.phone, "created_at": u.created_at.isoformat()} for u in rows]


@router.get("/appointments")
def list_all_appointments(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    stmt = select(Appointment).order_by(Appointment.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Appointment.status == status)
    rows = db.scalars(stmt).all()
    return [
        {
            "id": a.id,
            "patient": a.patient.full_name if a.patient else None,
            "patient_email": a.patient.email if a.patient else None,
            "doctor": a.doctor.user.full_name if a.doctor and a.doctor.user else None,
            "specialisation": a.doctor.specialisation if a.doctor else None,
            "status": a.status,
            "scheduled_start": a.scheduled_start.isoformat() if a.scheduled_start else None,
        }
        for a in rows
    ]


# --------------------------------------------------------------------------
# Email outbox / dead-letter inspection
# --------------------------------------------------------------------------
@router.get("/outbox")
def list_outbox(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(EmailOutbox).order_by(EmailOutbox.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(EmailOutbox.status == status)
    rows = db.scalars(stmt).all()
    return [
        {
            "id": r.id,
            "to_email": r.to_email,
            "subject": r.subject,
            "kind": r.kind,
            "status": r.status,
            "attempts": r.attempts,
            "max_attempts": r.max_attempts,
            "last_error": r.last_error,
            "created_at": r.created_at.isoformat(),
            "sent_at": r.sent_at.isoformat() if r.sent_at else None,
        }
        for r in rows
    ]
