"""Google Calendar sync tasks.

Each task loads the appointment, mirrors the change onto the patient's and the
doctor's calendars (whichever are connected), and persists the resulting event
ids. All underlying calendar calls are safe no-ops when Google is unconfigured
or a user hasn't connected, so these tasks never raise into the caller.
"""
from __future__ import annotations

import logging

from ..celery_app import celery_app
from ..database import SessionLocal
from ..models import Appointment
from ..services import calendar as gcal

logger = logging.getLogger(__name__)


def _times(appt: Appointment):
    start = appt.scheduled_start
    end = appt.scheduled_end or (appt.slot.end_time if appt.slot else None) or start
    return start, end


@celery_app.task(name="calendar.create")
def create_calendar_events_task(appointment_id: int) -> str:
    if not gcal.is_configured():
        return "unconfigured"
    db = SessionLocal()
    try:
        appt = db.get(Appointment, appointment_id)
        if appt is None or appt.scheduled_start is None:
            return "missing"
        start, end = _times(appt)
        doctor_user = appt.doctor.user
        patient = appt.patient

        patient_event = gcal.create_event_for_user(
            db, patient.id,
            summary=f"Appointment with Dr. {doctor_user.full_name}",
            description="Booked via the clinic portal.",
            start_dt=start, end_dt=end,
        )
        doctor_event = gcal.create_event_for_user(
            db, doctor_user.id,
            summary=f"Appointment with {patient.full_name}",
            description=(appt.symptoms or "")[:500],
            start_dt=start, end_dt=end,
        )
        appt = db.get(Appointment, appointment_id)
        if patient_event:
            appt.google_event_id_patient = patient_event
        if doctor_event:
            appt.google_event_id_doctor = doctor_event
        db.commit()
        return "ok"
    finally:
        db.close()


@celery_app.task(name="calendar.update")
def update_calendar_events_task(appointment_id: int) -> str:
    if not gcal.is_configured():
        return "unconfigured"
    db = SessionLocal()
    try:
        appt = db.get(Appointment, appointment_id)
        if appt is None or appt.scheduled_start is None:
            return "missing"
        start, end = _times(appt)
        if appt.google_event_id_patient:
            gcal.update_event_for_user(db, appt.patient_id, appt.google_event_id_patient, start_dt=start, end_dt=end)
        if appt.google_event_id_doctor:
            gcal.update_event_for_user(db, appt.doctor.user_id, appt.google_event_id_doctor, start_dt=start, end_dt=end)
        return "ok"
    finally:
        db.close()


@celery_app.task(name="calendar.delete")
def delete_calendar_events_task(appointment_id: int) -> str:
    if not gcal.is_configured():
        return "unconfigured"
    db = SessionLocal()
    try:
        appt = db.get(Appointment, appointment_id)
        if appt is None:
            return "missing"
        if appt.google_event_id_patient:
            if gcal.delete_event_for_user(db, appt.patient_id, appt.google_event_id_patient):
                appt.google_event_id_patient = None
        if appt.google_event_id_doctor:
            if gcal.delete_event_for_user(db, appt.doctor.user_id, appt.google_event_id_doctor):
                appt.google_event_id_doctor = None
        db.commit()
        return "ok"
    finally:
        db.close()
