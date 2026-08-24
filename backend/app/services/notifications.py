"""Notification facade.

One place that turns domain events (booking confirmed, cancelled, rescheduled,
visit completed, reminders due) into:

* a durable **email** via the outbox (``services.email.enqueue_email``), and
* a **Google Calendar** side effect via a Celery task (lazily imported to avoid
  an import cycle, and so it runs async in the real topology / inline in eager
  mode).

Routers and tasks call these *after* the booking transaction commits, keeping
``services.booking`` pure. Every function is best-effort: a failure to enqueue
never propagates into the request path.
"""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from .booking import AffectedAppointment
from ..config import settings
from ..models import Appointment
from ..schemas import PostVisitSummary
from .email import enqueue_email

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def _fmt(when: dt.datetime | None) -> str:
    if when is None:
        return "your scheduled time"
    local = when.astimezone(ZoneInfo(settings.CLINIC_TZ))
    return local.strftime("%A %d %b %Y, %I:%M %p")


def _doctor_name(appt: Appointment) -> str:
    try:
        return appt.doctor.user.full_name
    except Exception:  # noqa: BLE001 - relationship not loaded / detached
        return "your doctor"


def _doctor_email(appt: Appointment) -> str | None:
    try:
        email = appt.doctor.user.email  # type: ignore[union-attr]
        return email if email else None
    except Exception:  # noqa: BLE001
        return None


def _clinic() -> str:
    return settings.EMAIL_FROM_NAME


# --------------------------------------------------------------------------
# Calendar task triggers (lazy import; no-op if unavailable)
# --------------------------------------------------------------------------
def _trigger_calendar(action: str, appointment_id: int) -> None:
    try:
        from ..tasks import calendar as caltasks

        task = {
            "create": caltasks.create_calendar_events_task,
            "update": caltasks.update_calendar_events_task,
            "delete": caltasks.delete_calendar_events_task,
        }[action]
        task.delay(appointment_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("calendar trigger %s for appt %s skipped (%s)", action, appointment_id, exc)


# --------------------------------------------------------------------------
# Patient-facing events
# --------------------------------------------------------------------------
def notify_booking_confirmed(appt: Appointment) -> None:
    when = _fmt(appt.scheduled_start)
    subject = "Your appointment is confirmed"
    # Patient email
    text_patient = (
        f"Hi {appt.patient.full_name},\n\n"
        f"Your appointment with Dr. {_doctor_name(appt)} is confirmed for {when}.\n\n"
        f"Please arrive a few minutes early. You can view or manage this appointment "
        f"from your patient portal.\n\n— {_clinic()}"
    )
    enqueue_email(
        to_email=appt.patient.email,
        subject=subject,
        text=text_patient,
        kind="booking_confirmed",
        dedupe_key=f"appt:{appt.id}:confirmed",
        related_appointment_id=appt.id,
    )
    # Doctor email (mirrored)
    doctor_email = _doctor_email(appt)
    if doctor_email:
        text_doctor = (
            f"Hi Dr. {_doctor_name(appt)},\n\n"
            f"New appointment confirmed with {appt.patient.full_name} for {when}.\n\n"
            f"Please check your schedule.\n\n— {_clinic()}"
        )
        enqueue_email(
            to_email=doctor_email,
            subject=f"New appointment: {appt.patient.full_name} — {when}",
            text=text_doctor,
            kind="booking_confirmed_doctor",
            dedupe_key=f"appt:{appt.id}:confirmed:doctor",
            related_appointment_id=appt.id,
        )
    _trigger_calendar("create", appt.id)


def notify_reschedule(appt: Appointment) -> None:
    when = _fmt(appt.scheduled_start)
    subject = "Your appointment has been rescheduled"
    key_time = (appt.scheduled_start or dt.datetime.now(dt.timezone.utc)).isoformat()
    # Patient
    text_patient = (
        f"Hi {appt.patient.full_name},\n\n"
        f"Your appointment with Dr. {_doctor_name(appt)} is now scheduled for {when}.\n\n"
        f"— {_clinic()}"
    )
    enqueue_email(
        to_email=appt.patient.email,
        subject=subject,
        text=text_patient,
        kind="reschedule",
        dedupe_key=f"appt:{appt.id}:resched:{key_time}",
        related_appointment_id=appt.id,
    )
    # Doctor
    doctor_email = _doctor_email(appt)
    if doctor_email:
        text_doctor = (
            f"Hi Dr. {_doctor_name(appt)},\n\n"
            f"Appointment with {appt.patient.full_name} rescheduled to {when}.\n\n"
            f"— {_clinic()}"
        )
        enqueue_email(
            to_email=doctor_email,
            subject=f"Appointment rescheduled: {appt.patient.full_name} — {when}",
            text=text_doctor,
            kind="reschedule_doctor",
            dedupe_key=f"appt:{appt.id}:resched:doctor:{key_time}",
            related_appointment_id=appt.id,
        )
    _trigger_calendar("update", appt.id)


def _send_cancellation(
    *, to_email: str, patient_name: str, doctor_name: str, when: str,
    appointment_id: int, dedupe_key: str, reason: str | None,
) -> None:
    why = f"\n\nReason: {reason}" if reason else ""
    text = (
        f"Hi {patient_name},\n\n"
        f"Your appointment with Dr. {doctor_name} for {when} has been cancelled.{why}\n\n"
        f"You can book a new appointment any time from your patient portal.\n\n— {_clinic()}"
    )
    enqueue_email(
        to_email=to_email,
        subject="Your appointment has been cancelled",
        text=text,
        kind="cancellation",
        dedupe_key=dedupe_key,
        related_appointment_id=appointment_id,
    )
    _trigger_calendar("delete", appointment_id)


def notify_cancellation(appt: Appointment, *, reason: str | None = None) -> None:
    when = _fmt(appt.scheduled_start)
    # Patient
    _send_cancellation(
        to_email=appt.patient.email,
        patient_name=appt.patient.full_name,
        doctor_name=_doctor_name(appt),
        when=when,
        appointment_id=appt.id,
        dedupe_key=f"appt:{appt.id}:cancelled",
        reason=reason,
    )
    # Doctor (calendar delete already triggered for patient; trigger again is idempotent via dedupe)
    doctor_email = _doctor_email(appt)
    if doctor_email:
        why = f"\n\nReason: {reason}" if reason else ""
        text_doctor = (
            f"Hi Dr. {_doctor_name(appt)},\n\n"
            f"Appointment with {appt.patient.full_name} for {when} has been cancelled.{why}\n\n"
            f"— {_clinic()}"
        )
        enqueue_email(
            to_email=doctor_email,
            subject=f"Appointment cancelled: {appt.patient.full_name} — {when}",
            text=text_doctor,
            kind="cancellation_doctor",
            dedupe_key=f"appt:{appt.id}:cancelled:doctor",
            related_appointment_id=appt.id,
        )
        # Ensure calendar delete is triggered even if patient path de-duplicated
        _trigger_calendar("delete", appt.id)


def notify_leave_cancellations(affected: list[AffectedAppointment], leave_date: dt.date) -> None:
    """Cancellation notices for every appointment hit by a doctor's leave."""
    reason = "the doctor is unavailable on this date"
    for a in affected:
        # Patient notice (with calendar delete via _send_cancellation)
        _send_cancellation(
            to_email=a.patient_email,
            patient_name=a.patient_name,
            doctor_name=a.doctor_name,
            when=_fmt(a.start_time),
            appointment_id=a.appointment_id,
            dedupe_key=f"appt:{a.appointment_id}:leave:{leave_date.isoformat()}",
            reason=reason,
        )
        # Doctor summary per affected appointment
        if getattr(a, "doctor_email", None):
            when = _fmt(a.start_time)
            enqueue_email(
                to_email=a.doctor_email,  # type: ignore[attr-defined]
                subject=f"Appointment cancelled (leave {leave_date.isoformat()}): {a.patient_name} — {when}",
                text=(
                    f"Hi Dr. {a.doctor_name},\n\n"
                    f"Appointment with {a.patient_name} for {when} has been cancelled due to leave on {leave_date.isoformat()}.\n\n"
                    f"— {_clinic()}"
                ),
                kind="cancellation_doctor",
                dedupe_key=f"appt:{a.appointment_id}:leave:{leave_date.isoformat()}:doctor",
                related_appointment_id=a.appointment_id,
            )
        # Leave calendar delete already triggered by _send_cancellation; kept explicit for clarity


def notify_postvisit_ready(appt: Appointment, summary: PostVisitSummary) -> None:
    lines = [
        f"Hi {appt.patient.full_name},",
        "",
        f"Here is a summary of your visit with Dr. {_doctor_name(appt)}:",
        "",
        summary.summary_text,
    ]
    if summary.medication_schedule:
        lines += ["", "Medication schedule:"] + [f"  • {m}" for m in summary.medication_schedule]
    if summary.follow_up_steps:
        lines += ["", "Follow-up steps:"] + [f"  • {s}" for s in summary.follow_up_steps]
    lines += ["", f"— {_clinic()}"]
    enqueue_email(
        to_email=appt.patient.email,
        subject="Your visit summary and next steps",
        text="\n".join(lines),
        kind="postvisit_summary",
        dedupe_key=f"appt:{appt.id}:postvisit",
        related_appointment_id=appt.id,
    )
    # Also notify doctor that summary was delivered (optional confirmation)
    doctor_email = _doctor_email(appt)
    if doctor_email:
        enqueue_email(
            to_email=doctor_email,
            subject=f"Visit summary sent: {appt.patient.full_name}",
            text=(
                f"Hi Dr. {_doctor_name(appt)},\n\n"
                f"Post-visit summary for {appt.patient.full_name} has been sent to the patient.\n\n"
                f"— {_clinic()}"
            ),
            kind="postvisit_summary_doctor",
            dedupe_key=f"appt:{appt.id}:postvisit:doctor",
            related_appointment_id=appt.id,
        )


def notify_appointment_reminder(appt: Appointment) -> None:
    when = _fmt(appt.scheduled_start)
    # Per-time dedupe so rescheduled appointments re-remind
    key_time = (appt.scheduled_start or dt.datetime.now(dt.timezone.utc)).isoformat()
    # Patient
    text_patient = (
        f"Hi {appt.patient.full_name},\n\n"
        f"This is a reminder of your upcoming appointment with Dr. {_doctor_name(appt)} "
        f"on {when}.\n\n— {_clinic()}"
    )
    enqueue_email(
        to_email=appt.patient.email,
        subject="Appointment reminder",
        text=text_patient,
        kind="appointment_reminder",
        dedupe_key=f"appt:{appt.id}:reminder:{key_time}",
        related_appointment_id=appt.id,
    )
    # Doctor
    doctor_email = _doctor_email(appt)
    if doctor_email:
        text_doctor = (
            f"Hi Dr. {_doctor_name(appt)},\n\n"
            f"Reminder: appointment with {appt.patient.full_name} on {when}.\n\n— {_clinic()}"
        )
        enqueue_email(
            to_email=doctor_email,
            subject=f"Reminder: {appt.patient.full_name} — {when}",
            text=text_doctor,
            kind="appointment_reminder_doctor",
            dedupe_key=f"appt:{appt.id}:reminder:doctor:{key_time}",
            related_appointment_id=appt.id,
        )


def notify_medication_reminder(*, to_email: str, patient_name: str, medication: str,
                               dosage: str | None, reminder_id: int) -> None:
    dose = f" ({dosage})" if dosage else ""
    text = (
        f"Hi {patient_name},\n\n"
        f"It's time to take your medication: {medication}{dose}.\n\n"
        f"— {_clinic()}"
    )
    enqueue_email(
        to_email=to_email,
        subject=f"Medication reminder: {medication}",
        text=text,
        kind="medication_reminder",
        dedupe_key=f"medrem:{reminder_id}",
    )
