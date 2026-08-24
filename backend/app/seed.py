"""Idempotent database seeding for demo / local development.

Creates the admin (from ``SEED_ADMIN_EMAIL`` / ``SEED_ADMIN_PASSWORD``), one
sample doctor with weekday working hours, and one sample patient, then
materialises the doctor's slot window so the clinic is immediately bookable.

Safe to run repeatedly — every entity is looked up by its natural key first.

Usage:
    python -m app.seed
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from .config import settings
from .database import Base, SessionLocal, engine
from .models import (
    ROLE_ADMIN,
    ROLE_DOCTOR,
    ROLE_PATIENT,
    DoctorProfile,
    DoctorWorkingHours,
    User,
)
from .services import booking
from .services.accounts import register_user

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger("app.seed")

# Demo credentials (documented in the README).
DOCTOR_EMAIL = "dr.rao@clinic.test"
DOCTOR_PASSWORD = "doctor12345"
PATIENT_EMAIL = "patient@clinic.test"
PATIENT_PASSWORD = "patient12345"

# Mon–Fri 09:00–17:00 in the clinic timezone.
WEEKDAY_HOURS = [
    (day, dt.time(9, 0), dt.time(17, 0)) for day in range(0, 5)
]


def _get_or_create_user(db, *, email, password, full_name, role, phone=None) -> tuple[User, bool]:
    existing = db.scalar(select(User).where(User.email == email.lower()))
    if existing is not None:
        return existing, False
    user = register_user(
        db, email=email, password=password, full_name=full_name, phone=phone, role=role
    )
    return user, True


def seed() -> None:
    # For SQLite (dev/tests) create tables directly; Postgres uses Alembic.
    if settings.is_sqlite:
        Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # ---- Admin ----
        admin, created = _get_or_create_user(
            db,
            email=settings.SEED_ADMIN_EMAIL,
            password=settings.SEED_ADMIN_PASSWORD,
            full_name="Clinic Administrator",
            role=ROLE_ADMIN,
        )
        logger.info("admin %s (%s)", admin.email, "created" if created else "exists")

        # ---- Sample doctor + profile + working hours ----
        doc_user, created = _get_or_create_user(
            db,
            email=DOCTOR_EMAIL,
            password=DOCTOR_PASSWORD,
            full_name="Dr. Anita Rao",
            role=ROLE_DOCTOR,
            phone="+91-99999-00001",
        )
        profile = db.scalar(select(DoctorProfile).where(DoctorProfile.user_id == doc_user.id))
        if profile is None:
            profile = DoctorProfile(
                user_id=doc_user.id,
                specialisation="Cardiology",
                bio="Consultant cardiologist. Special interest in preventive heart care.",
                slot_duration_min=30,
                active=True,
            )
            db.add(profile)
            db.commit()
            db.refresh(profile)
        logger.info("doctor %s (profile id=%s)", doc_user.email, profile.id)

        # Working hours (idempotent: only set when none exist).
        has_hours = db.scalar(
            select(DoctorWorkingHours.id).where(DoctorWorkingHours.doctor_id == profile.id)
        )
        if has_hours is None:
            for day, start, end in WEEKDAY_HOURS:
                db.add(
                    DoctorWorkingHours(
                        doctor_id=profile.id, day_of_week=day, start_time=start, end_time=end
                    )
                )
            db.commit()
            logger.info("working hours set (Mon-Fri 09:00-17:00 %s)", settings.CLINIC_TZ)

        # ---- Sample patient ----
        patient, created = _get_or_create_user(
            db,
            email=PATIENT_EMAIL,
            password=PATIENT_PASSWORD,
            full_name="Sam Patient",
            role=ROLE_PATIENT,
            phone="+91-99999-00002",
        )
        logger.info("patient %s (%s)", patient.email, "created" if created else "exists")

        # ---- Materialise the bookable slot window ----
        booking.regenerate_future_free_slots(db, profile.id)
        logger.info("slot window generated for doctor %s", profile.id)

    finally:
        db.close()

    print(
        "\nSeed complete. Demo accounts:\n"
        f"  Admin   : {settings.SEED_ADMIN_EMAIL} / {settings.SEED_ADMIN_PASSWORD}\n"
        f"  Doctor  : {DOCTOR_EMAIL} / {DOCTOR_PASSWORD}\n"
        f"  Patient : {PATIENT_EMAIL} / {PATIENT_PASSWORD}\n"
    )


if __name__ == "__main__":
    seed()
