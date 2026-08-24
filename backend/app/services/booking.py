"""Booking service — slot materialisation + the lock-free CAS state machine.

This module owns the correctness of double-booking prevention. The ``slots``
row is the unit of concurrency control; every transition is a single guarded
``UPDATE`` whose ``rowcount`` is the verdict:

    L1  Redis lock (optimisation, fail-open)         infra.locks.slot_lock
    L2  CAS UPDATE ... WHERE status = <expected>     rowcount == 1  → win
    L3  UNIQUE (doctor_id, start_time)               integrity net

No ``SELECT FOR UPDATE`` anywhere, so behaviour is identical on SQLite and
Postgres. Side effects (email, calendar) are NOT done here — callers enqueue
them after the transaction commits, keeping this layer pure and testable.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..database import utcnow
from ..errors import Conflict, Forbidden, NotFound
from ..infra.cache import bump_doctor_version, cached_json, doctor_version
from ..infra.locks import slot_lock
from ..models import (
    APPT_CANCELLED,
    APPT_COMPLETED,
    APPT_CONFIRMED,
    APPT_EXPIRED,
    APPT_HOLDING,
    SLOT_BLOCKED,
    SLOT_BOOKED,
    SLOT_FREE,
    SLOT_HELD,
    Appointment,
    DoctorLeave,
    DoctorProfile,
    Slot,
)

logger = logging.getLogger(__name__)
UTC = dt.timezone.utc


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def _clinic_tz() -> ZoneInfo:
    return ZoneInfo(settings.CLINIC_TZ)


def _day_bounds_utc(d: dt.date) -> tuple[dt.datetime, dt.datetime]:
    tz = _clinic_tz()
    start_local = dt.datetime.combine(d, dt.time.min).replace(tzinfo=tz)
    end_local = start_local + dt.timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _is_leave(db: Session, doctor_id: int, d: dt.date) -> bool:
    return db.scalar(
        select(DoctorLeave.id).where(
            DoctorLeave.doctor_id == doctor_id, DoctorLeave.leave_date == d
        )
    ) is not None


# --------------------------------------------------------------------------
# Slot materialisation (idempotent)
# --------------------------------------------------------------------------
def _generate_rows(doctor: DoctorProfile, d: dt.date) -> list[dict]:
    tz = _clinic_tz()
    now = utcnow()
    dur = dt.timedelta(minutes=doctor.slot_duration_min)
    rows: list[dict] = []
    for wh in doctor.working_hours:
        if wh.day_of_week != d.weekday():
            continue
        cur = dt.datetime.combine(d, wh.start_time).replace(tzinfo=tz)
        end = dt.datetime.combine(d, wh.end_time).replace(tzinfo=tz)
        while cur + dur <= end:
            s_utc = cur.astimezone(UTC)
            e_utc = (cur + dur).astimezone(UTC)
            if s_utc > now:  # never generate past slots
                rows.append(
                    {
                        "doctor_id": doctor.id,
                        "start_time": s_utc,
                        "end_time": e_utc,
                        "status": SLOT_FREE,
                        "version": 0,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            cur += dur
    return rows


def _insert_ignore(db: Session, rows: list[dict]) -> None:
    """Insert slot rows ignoring conflicts on (doctor_id, start_time).

    Uses the dialect-native ``ON CONFLICT DO NOTHING`` (both SQLite and
    Postgres support it) so concurrent generation never duplicates or errors.
    """
    if not rows:
        return
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(Slot).values(rows).on_conflict_do_nothing(
            index_elements=["doctor_id", "start_time"]
        )
        db.execute(stmt)
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(Slot).values(rows).on_conflict_do_nothing(
            index_elements=["doctor_id", "start_time"]
        )
        db.execute(stmt)
    else:  # portable fallback: per-row savepoint
        for row in rows:
            try:
                with db.begin_nested():
                    db.execute(insert(Slot).values(**row))
            except Exception:  # noqa: BLE001 - unique violation is expected/ignored
                pass


def ensure_slots_for_date(db: Session, doctor_id: int, d: dt.date) -> None:
    """Lazily materialise a doctor's slots for one date (idempotent)."""
    doctor = db.get(DoctorProfile, doctor_id)
    if doctor is None or not doctor.active:
        return
    if _is_leave(db, doctor_id, d):
        return
    _insert_ignore(db, _generate_rows(doctor, d))
    db.commit()


def generate_rolling_window(db: Session, days: int | None = None) -> int:
    """Pre-generate slots for all active doctors over the rolling horizon."""
    days = days or settings.SLOT_WINDOW_DAYS
    today = utcnow().astimezone(_clinic_tz()).date()
    doctors = db.scalars(select(DoctorProfile).where(DoctorProfile.active.is_(True))).all()
    count = 0
    for doctor in doctors:
        for offset in range(days):
            ensure_slots_for_date(db, doctor.id, today + dt.timedelta(days=offset))
            count += 1
    return count


def regenerate_future_free_slots(db: Session, doctor_id: int) -> None:
    """Rebuild future FREE slots after a working-hours change.

    Held/booked slots are preserved (never silently dropped). A booked slot now
    outside the new hours simply remains a valid appointment and is logged.
    """
    now = utcnow()
    db.execute(
        delete(Slot).where(
            Slot.doctor_id == doctor_id, Slot.status == SLOT_FREE, Slot.start_time > now
        )
    )
    db.commit()
    today = now.astimezone(_clinic_tz()).date()
    for offset in range(settings.SLOT_WINDOW_DAYS):
        ensure_slots_for_date(db, doctor_id, today + dt.timedelta(days=offset))
    bump_doctor_version(doctor_id)


# --------------------------------------------------------------------------
# Availability (cached)
# --------------------------------------------------------------------------
def get_free_slots(db: Session, doctor_id: int, d: dt.date) -> list[Slot]:
    ensure_slots_for_date(db, doctor_id, d)
    start_utc, end_utc = _day_bounds_utc(d)
    now = utcnow()
    stmt = (
        select(Slot)
        .where(
            Slot.doctor_id == doctor_id,
            Slot.status == SLOT_FREE,
            Slot.start_time >= start_utc,
            Slot.start_time < end_utc,
            Slot.start_time > now,
        )
        .order_by(Slot.start_time)
    )
    return list(db.scalars(stmt).all())


def available_slots_payload(db: Session, doctor_id: int, date_str: str) -> list[dict]:
    """Cache-aside list of available slots (stable JSON, versioned key)."""
    ver = doctor_version(doctor_id)
    key = f"cache:slots:{doctor_id}:{date_str}:{ver}"

    def producer() -> list[dict]:
        d = dt.date.fromisoformat(date_str)
        return [
            {
                "id": s.id,
                "doctor_id": s.doctor_id,
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
                "status": s.status,
            }
            for s in get_free_slots(db, doctor_id, d)
        ]

    return cached_json(key, ttl=60, producer=producer)


# --------------------------------------------------------------------------
# The CAS state machine: hold → confirm → cancel / reschedule / expire
# --------------------------------------------------------------------------
def _live_self_hold(db: Session, patient_id: int, slot: Slot) -> Appointment | None:
    """The HOLDING appointment this patient already has on ``slot``, if any.

    Natural idempotency: "hold slot X" is a request for a *state*, and if the
    caller is already in that state the honest answer is success, not 409. This
    matters because the ``Idempotency-Key`` replay cache lives in Redis and
    degrades to a no-op when Redis is down — without this, a double-clicked
    "Book" during an outage told the patient the slot "was just taken" by the
    very hold they had themselves just placed.
    """
    if slot.status != SLOT_HELD or slot.held_by != patient_id:
        return None
    if slot.hold_expires_at is None or slot.hold_expires_at <= utcnow():
        return None  # expired: let the caller get a Conflict and pick again
    if slot.appointment_id is None:
        return None
    appt = db.get(Appointment, slot.appointment_id)
    if appt is None or appt.status != APPT_HOLDING or appt.patient_id != patient_id:
        return None
    return appt


def hold_slot(db: Session, patient_id: int, slot_id: int) -> Appointment:
    """Phase 1: reserve a FREE slot for this patient (free → held)."""
    slot = db.get(Slot, slot_id)
    if slot is None:
        raise NotFound("Slot not found")
    if slot.status != SLOT_FREE:
        mine = _live_self_hold(db, patient_id, slot)
        if mine is not None:
            return mine
        raise Conflict("Slot is not available")
    if slot.start_time <= utcnow():
        raise Conflict("Slot is in the past")

    with slot_lock(slot_id) as got_lock:
        if not got_lock:
            raise Conflict("Slot is currently being booked by someone else")

        expires = utcnow() + dt.timedelta(minutes=settings.HOLD_MINUTES)
        # L2 CAS — the guarantee.
        res = db.execute(
            update(Slot)
            .where(Slot.id == slot_id, Slot.status == SLOT_FREE)
            .values(
                status=SLOT_HELD,
                held_by=patient_id,
                hold_expires_at=expires,
                version=Slot.version + 1,
            )
        )
        if res.rowcount != 1:
            db.rollback()
            # Truly concurrent double-click: the sibling request won the CAS a
            # microsecond ago. Re-read and hand back *our own* hold if that is
            # who beat us, so only a different patient produces a Conflict.
            fresh = db.get(Slot, slot_id)
            if fresh is not None:
                mine = _live_self_hold(db, patient_id, fresh)
                if mine is not None:
                    return mine
            raise Conflict("Slot was just taken; please pick another")

        appt = Appointment(
            slot_id=slot_id,
            doctor_id=slot.doctor_id,
            patient_id=patient_id,
            status=APPT_HOLDING,
            scheduled_start=slot.start_time,
            scheduled_end=slot.end_time,
        )
        db.add(appt)
        db.flush()  # obtain appt.id
        db.execute(update(Slot).where(Slot.id == slot_id).values(appointment_id=appt.id))
        db.commit()
        db.refresh(appt)

    bump_doctor_version(slot.doctor_id)
    return appt


def set_symptoms(db: Session, patient_id: int, appointment_id: int, symptoms: str) -> Appointment:
    appt = _load_owned_appointment(db, patient_id, appointment_id)
    if appt.status not in (APPT_HOLDING, APPT_CONFIRMED):
        raise Conflict("Cannot attach symptoms to this appointment")
    appt.symptoms = symptoms
    db.commit()
    db.refresh(appt)
    return appt


def confirm_appointment(db: Session, patient_id: int, appointment_id: int) -> Appointment:
    """Phase 2: commit a held slot (held → booked), guarded by owner + expiry."""
    appt = _load_owned_appointment(db, patient_id, appointment_id)
    if appt.status == APPT_CONFIRMED:
        return appt  # idempotent
    if appt.status != APPT_HOLDING:
        raise Conflict("Appointment is not in a holdable state")

    now = utcnow()
    res = db.execute(
        update(Slot)
        .where(
            Slot.id == appt.slot_id,
            Slot.status == SLOT_HELD,
            Slot.held_by == patient_id,
            Slot.hold_expires_at > now,
        )
        .values(status=SLOT_BOOKED, hold_expires_at=None, version=Slot.version + 1)
    )
    if res.rowcount != 1:
        db.rollback()
        raise Conflict("Your hold expired — please pick a slot again")

    appt.status = APPT_CONFIRMED
    db.commit()
    db.refresh(appt)
    bump_doctor_version(appt.doctor_id)
    return appt


def cancel_appointment(db: Session, appointment_id: int, *, by_user_id: int, is_admin: bool = False) -> Appointment:
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise NotFound("Appointment not found")
    if not is_admin and appt.patient_id != by_user_id:
        raise Forbidden("Not your appointment")
    if appt.status in (APPT_CANCELLED, APPT_EXPIRED, APPT_COMPLETED):
        return appt

    slot_id = appt.slot_id
    _free_slot(db, slot_id)
    appt.status = APPT_CANCELLED
    appt.slot_id = None  # detach so the slot can be recycled
    db.commit()
    db.refresh(appt)
    bump_doctor_version(appt.doctor_id)
    return appt


def reschedule_appointment(db: Session, patient_id: int, appointment_id: int, new_slot_id: int) -> Appointment:
    appt = _load_owned_appointment(db, patient_id, appointment_id)
    if appt.status not in (APPT_HOLDING, APPT_CONFIRMED):
        raise Conflict("Only active appointments can be rescheduled")

    new_slot = db.get(Slot, new_slot_id)
    if new_slot is None:
        raise NotFound("New slot not found")
    if new_slot.doctor_id != appt.doctor_id:
        raise Conflict("Rescheduling must stay with the same doctor")
    if new_slot.start_time <= utcnow():
        raise Conflict("New slot is in the past")

    target_status = SLOT_BOOKED if appt.status == APPT_CONFIRMED else SLOT_HELD
    old_slot_id = appt.slot_id

    with slot_lock(new_slot_id) as got_lock:
        if not got_lock:
            raise Conflict("That slot is currently being booked by someone else")
        expires = None if target_status == SLOT_BOOKED else utcnow() + dt.timedelta(minutes=settings.HOLD_MINUTES)
        res = db.execute(
            update(Slot)
            .where(Slot.id == new_slot_id, Slot.status == SLOT_FREE)
            .values(
                status=target_status,
                held_by=patient_id,
                hold_expires_at=expires,
                appointment_id=appt.id,
                version=Slot.version + 1,
            )
        )
        if res.rowcount != 1:
            db.rollback()
            raise Conflict("That slot is no longer available")  # old booking untouched

        # Only after the new slot is secured do we release the old one.
        _free_slot(db, old_slot_id)
        appt.slot_id = new_slot_id
        appt.scheduled_start = new_slot.start_time
        appt.scheduled_end = new_slot.end_time
        db.commit()
        db.refresh(appt)

    bump_doctor_version(appt.doctor_id)
    return appt


def expire_stale_holds(db: Session) -> list[int]:
    """Beat job: return expired holds to FREE. Returns affected appointment ids."""
    now = utcnow()
    stale = db.scalars(
        select(Slot).where(Slot.status == SLOT_HELD, Slot.hold_expires_at < now)
    ).all()
    affected: list[int] = []
    doctor_ids: set[int] = set()
    for slot in stale:
        appt_id = slot.appointment_id
        res = db.execute(
            update(Slot)
            .where(Slot.id == slot.id, Slot.status == SLOT_HELD, Slot.hold_expires_at < now)
            .values(
                status=SLOT_FREE,
                held_by=None,
                hold_expires_at=None,
                appointment_id=None,
                version=Slot.version + 1,
            )
        )
        if res.rowcount == 1 and appt_id:
            appt = db.get(Appointment, appt_id)
            if appt and appt.status == APPT_HOLDING:
                appt.status = APPT_EXPIRED
                appt.slot_id = None
                affected.append(appt.id)
            doctor_ids.add(slot.doctor_id)
    db.commit()
    for did in doctor_ids:
        bump_doctor_version(did)
    return affected


# --------------------------------------------------------------------------
# Leave handling
# --------------------------------------------------------------------------
@dataclass
class AffectedAppointment:
    appointment_id: int
    patient_id: int
    patient_email: str
    patient_name: str
    doctor_name: str
    doctor_email: str | None
    start_time: dt.datetime
    event_id_patient: str | None
    event_id_doctor: str | None


def apply_leave(db: Session, doctor_id: int, leave_date: dt.date, reason: str | None) -> list[AffectedAppointment]:
    """Mark a leave day: cancel affected appointments and block the day's slots.

    Idempotent on (doctor_id, leave_date). Returns the affected appointments so
    the caller can enqueue cancellation emails + calendar deletions.
    """
    doctor = db.get(DoctorProfile, doctor_id)
    if doctor is None:
        raise NotFound("Doctor not found")

    # Idempotent leave record.
    if not _is_leave(db, doctor_id, leave_date):
        db.add(DoctorLeave(doctor_id=doctor_id, leave_date=leave_date, reason=reason))
        db.flush()

    start_utc, end_utc = _day_bounds_utc(leave_date)

    # Collect + cancel affected appointments (held/booked that day).
    affected: list[AffectedAppointment] = []
    appts = db.scalars(
        select(Appointment)
        .join(Slot, Appointment.slot_id == Slot.id)
        .where(
            Slot.doctor_id == doctor_id,
            Slot.start_time >= start_utc,
            Slot.start_time < end_utc,
            Appointment.status.in_((APPT_HOLDING, APPT_CONFIRMED)),
        )
    ).all()
    for appt in appts:
        affected.append(
            AffectedAppointment(
                appointment_id=appt.id,
                patient_id=appt.patient_id,
                patient_email=appt.patient.email,
                patient_name=appt.patient.full_name,
                doctor_name=doctor.user.full_name,
                doctor_email=getattr(doctor.user, "email", None),
                start_time=appt.scheduled_start or (appt.slot.start_time if appt.slot else start_utc),
                event_id_patient=appt.google_event_id_patient,
                event_id_doctor=appt.google_event_id_doctor,
            )
        )
        appt.status = APPT_CANCELLED
        appt.slot_id = None

    # Block every free/held/booked slot that day.
    db.execute(
        update(Slot)
        .where(
            Slot.doctor_id == doctor_id,
            Slot.start_time >= start_utc,
            Slot.start_time < end_utc,
            Slot.status.in_((SLOT_FREE, SLOT_HELD, SLOT_BOOKED)),
        )
        .values(
            status=SLOT_BLOCKED,
            held_by=None,
            hold_expires_at=None,
            appointment_id=None,
            version=Slot.version + 1,
        )
    )
    db.commit()
    bump_doctor_version(doctor_id)
    return affected


def remove_leave(db: Session, doctor_id: int, leave_date: dt.date) -> None:
    """Cancel a leave: delete the record and reopen that day's blocked slots.

    Previously-cancelled appointments are NOT restored (patients were notified);
    the slots simply become bookable again.
    """
    db.execute(
        delete(DoctorLeave).where(
            DoctorLeave.doctor_id == doctor_id, DoctorLeave.leave_date == leave_date
        )
    )
    start_utc, end_utc = _day_bounds_utc(leave_date)
    db.execute(
        update(Slot)
        .where(
            Slot.doctor_id == doctor_id,
            Slot.start_time >= start_utc,
            Slot.start_time < end_utc,
            Slot.status == SLOT_BLOCKED,
        )
        .values(status=SLOT_FREE, version=Slot.version + 1)
    )
    db.commit()
    bump_doctor_version(doctor_id)


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------
def _free_slot(db: Session, slot_id: int | None) -> None:
    if slot_id is None:
        return
    db.execute(
        update(Slot)
        .where(Slot.id == slot_id)
        .values(
            status=SLOT_FREE,
            held_by=None,
            hold_expires_at=None,
            appointment_id=None,
            version=Slot.version + 1,
        )
    )


def _load_owned_appointment(db: Session, patient_id: int, appointment_id: int) -> Appointment:
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise NotFound("Appointment not found")
    if appt.patient_id != patient_id:
        raise Forbidden("Not your appointment")
    return appt
