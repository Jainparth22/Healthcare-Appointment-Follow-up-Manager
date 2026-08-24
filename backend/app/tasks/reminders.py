"""Reminder dispatch tasks (periodic).

* ``reminders.medications`` — sends due medication reminders and marks them
  ``sent``. Enqueue-then-mark ordering + the ``medrem:{id}`` dedupe key make it
  at-least-once with no duplicate emails.
* ``reminders.appointments`` — sends a reminder for every confirmed appointment
  starting within the next window. Idempotent via the outbox dedupe key, so
  re-running the sweep doesn't re-notify.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from ..celery_app import celery_app
from ..database import SessionLocal, utcnow
from ..models import (
    APPT_CONFIRMED,
    REMINDER_FAILED,
    REMINDER_PENDING,
    REMINDER_SENT,
    Appointment,
    MedicationReminder,
    Prescription,
    User,
)
from ..services.notifications import notify_appointment_reminder, notify_medication_reminder

APPT_REMINDER_WINDOW_HOURS = 24


@celery_app.task(name="reminders.medications")
def dispatch_medication_reminders_task(limit: int = 200) -> int:
    db = SessionLocal()
    try:
        now = utcnow()
        rows = db.scalars(
            select(MedicationReminder)
            .where(MedicationReminder.status == REMINDER_PENDING, MedicationReminder.scheduled_at <= now)
            .order_by(MedicationReminder.scheduled_at)
            .limit(limit)
        ).all()
        sent = 0
        for r in rows:
            presc = db.get(Prescription, r.prescription_id)
            patient = db.get(User, r.patient_id)
            if presc is None or patient is None:
                r.status = REMINDER_FAILED
                continue
            notify_medication_reminder(
                to_email=patient.email,
                patient_name=patient.full_name,
                medication=presc.medication_name,
                dosage=presc.dosage,
                reminder_id=r.id,
            )
            r.status = REMINDER_SENT
            r.sent_at = now
            sent += 1
        db.commit()
        return sent
    finally:
        db.close()


@celery_app.task(name="reminders.appointments")
def dispatch_appointment_reminders_task() -> int:
    db = SessionLocal()
    try:
        now = utcnow()
        horizon = now + dt.timedelta(hours=APPT_REMINDER_WINDOW_HOURS)
        appts = db.scalars(
            select(Appointment).where(
                Appointment.status == APPT_CONFIRMED,
                Appointment.scheduled_start > now,
                Appointment.scheduled_start <= horizon,
            )
        ).all()
        for appt in appts:
            notify_appointment_reminder(appt)
        return len(appts)
    finally:
        db.close()
