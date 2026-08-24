"""SQLAlchemy models.

Design highlight — the **slots / appointments split**:

* ``slots`` is the *inventory & concurrency* layer. A slot is the unit of the
  compare-and-swap (CAS) that prevents double-booking. Racing, holds, expiry
  and blocking all happen here.
* ``appointments`` is the *clinical record* layer (symptoms, notes, calendar
  event ids). It points at exactly one slot.

All enums are plain strings (portable across SQLite/Postgres). All timestamps
use :class:`UTCDateTime` (tz-aware UTC on both engines).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base, UTCDateTime, utcnow

# --------------------------------------------------------------------------
# Enum-like string constants (kept as plain strings for DB portability)
# --------------------------------------------------------------------------
ROLE_PATIENT = "patient"
ROLE_DOCTOR = "doctor"
ROLE_ADMIN = "admin"
ROLES = (ROLE_PATIENT, ROLE_DOCTOR, ROLE_ADMIN)

SLOT_FREE = "free"
SLOT_HELD = "held"
SLOT_BOOKED = "booked"
SLOT_BLOCKED = "blocked"

APPT_HOLDING = "holding"
APPT_CONFIRMED = "confirmed"
APPT_COMPLETED = "completed"
APPT_CANCELLED = "cancelled"
APPT_EXPIRED = "expired"

SUMMARY_PREVISIT = "previsit"
SUMMARY_POSTVISIT = "postvisit"
SUMMARY_OK = "ok"
SUMMARY_FALLBACK = "fallback"
SUMMARY_ERROR = "error"

OUTBOX_PENDING = "pending"
OUTBOX_SENT = "sent"
OUTBOX_FAILED = "failed"
OUTBOX_DEAD = "dead"

REMINDER_PENDING = "pending"
REMINDER_SENT = "sent"
REMINDER_FAILED = "failed"


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_PATIENT)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    doctor_profile: Mapped["DoctorProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------
# Doctor configuration
# --------------------------------------------------------------------------
class DoctorProfile(Base):
    __tablename__ = "doctor_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    specialisation: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    slot_duration_min: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="doctor_profile")
    working_hours: Mapped[list["DoctorWorkingHours"]] = relationship(
        back_populates="doctor", cascade="all, delete-orphan"
    )
    leaves: Mapped[list["DoctorLeave"]] = relationship(
        back_populates="doctor", cascade="all, delete-orphan"
    )


class DoctorWorkingHours(Base):
    __tablename__ = "doctor_working_hours"

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctor_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Mon .. 6=Sun
    start_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    end_time: Mapped[dt.time] = mapped_column(Time, nullable=False)

    doctor: Mapped[DoctorProfile] = relationship(back_populates="working_hours")

    __table_args__ = (
        UniqueConstraint("doctor_id", "day_of_week", "start_time", name="uq_wh_doctor_day_start"),
    )


class DoctorLeave(Base):
    __tablename__ = "doctor_leaves"

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctor_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    leave_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    doctor: Mapped[DoctorProfile] = relationship(back_populates="leaves")

    __table_args__ = (
        UniqueConstraint("doctor_id", "leave_date", name="uq_leave_doctor_date"),
    )


# --------------------------------------------------------------------------
# Slots — the inventory / concurrency layer (unit of CAS)
# --------------------------------------------------------------------------
class Slot(Base):
    __tablename__ = "slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctor_profiles.id", ondelete="CASCADE"), nullable=False
    )
    start_time: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    end_time: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=SLOT_FREE)

    # Hold bookkeeping
    held_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    hold_expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    appointment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Optimistic-concurrency version counter (bumped on every state transition).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        # L3 integrity net + makes slot generation idempotent.
        UniqueConstraint("doctor_id", "start_time", name="uq_slot_doctor_start"),
        Index("ix_slot_doctor_start", "doctor_id", "start_time"),
        Index("ix_slot_status_expiry", "status", "hold_expires_at"),
    )


# --------------------------------------------------------------------------
# Appointments — the clinical record layer
# --------------------------------------------------------------------------
class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable + unique: an appointment "occupies" a slot while active. On
    # cancel/expire we detach (slot_id -> NULL) so the slot can be recycled and
    # re-booked by a fresh appointment. UNIQUE permits many NULLs on both
    # SQLite and Postgres, so it still enforces "one active appointment / slot".
    slot_id: Mapped[int | None] = mapped_column(
        ForeignKey("slots.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctor_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=APPT_HOLDING)

    # Snapshot of the booked time so history survives slot detachment.
    scheduled_start: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    scheduled_end: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    symptoms: Mapped[str | None] = mapped_column(Text, nullable=True)
    doctor_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    google_event_id_patient: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_event_id_doctor: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    slot: Mapped[Slot] = relationship()
    doctor: Mapped[DoctorProfile] = relationship()
    patient: Mapped[User] = relationship()
    summaries: Mapped[list["Summary"]] = relationship(
        back_populates="appointment", cascade="all, delete-orphan"
    )
    prescriptions: Mapped[list["Prescription"]] = relationship(
        back_populates="appointment", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------
# LLM outputs (stored in DB per requirement)
# --------------------------------------------------------------------------
class Summary(Base):
    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # previsit | postvisit
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # structured JSON
    status: Mapped[str] = mapped_column(String(12), nullable=False)  # ok | fallback | error
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    appointment: Mapped[Appointment] = relationship(back_populates="summaries")


# --------------------------------------------------------------------------
# Prescriptions & medication reminders
# --------------------------------------------------------------------------
class Prescription(Base):
    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctor_profiles.id", ondelete="CASCADE"), nullable=False
    )
    medication_name: Mapped[str] = mapped_column(String(200), nullable=False)
    dosage: Mapped[str | None] = mapped_column(String(120), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(120), nullable=True)  # raw text
    times_per_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    appointment: Mapped[Appointment] = relationship(back_populates="prescriptions")
    reminders: Mapped[list["MedicationReminder"]] = relationship(
        back_populates="prescription", cascade="all, delete-orphan"
    )


class MedicationReminder(Base):
    __tablename__ = "medication_reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    prescription_id: Mapped[int] = mapped_column(
        ForeignKey("prescriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scheduled_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=REMINDER_PENDING)
    sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    prescription: Mapped[Prescription] = relationship(back_populates="reminders")

    __table_args__ = (
        UniqueConstraint("prescription_id", "scheduled_at", name="uq_reminder_presc_time"),
        Index("ix_reminder_status_time", "status", "scheduled_at"),
    )


# --------------------------------------------------------------------------
# Email outbox (at-least-once delivery with dedupe)
# --------------------------------------------------------------------------
class EmailOutbox(Base):
    __tablename__ = "email_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    to_email: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=OUTBOX_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    related_appointment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_outbox_status_sched", "status", "scheduled_at"),
    )


# --------------------------------------------------------------------------
# Google OAuth credentials (refresh token Fernet-encrypted at rest)
# --------------------------------------------------------------------------
class GoogleCredential(Base):
    __tablename__ = "google_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    enc_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    enc_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_uri: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    expiry: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
