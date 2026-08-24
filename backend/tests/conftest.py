"""Test configuration and shared fixtures.

CRITICAL: this module sets environment variables **before** importing anything
from ``app`` — the SQLAlchemy engine and settings singleton are built at import
time from ``DATABASE_URL``, so the override must land first. Tests run on a
throwaway file-backed SQLite DB with Celery eager, console email and no LLM key
(so the deterministic fallback path is exercised) and no Redis (infra degrades
gracefully — the DB CAS remains the booking guarantee).
"""
from __future__ import annotations

import os
import pathlib
import tempfile

# ---- Environment MUST be set before importing app.* ----
_TMPDIR = tempfile.mkdtemp(prefix="hcv-test-")
_DB_PATH = pathlib.Path(_TMPDIR) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "true"
os.environ["EMAIL_BACKEND"] = "console"
os.environ["GEMINI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/15"  # unreachable → degraded

import datetime as dt  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine, utcnow  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ROLE_ADMIN,
    ROLE_DOCTOR,
    ROLE_PATIENT,
    SLOT_FREE,
    DoctorProfile,
    DoctorWorkingHours,
    Slot,
)
from app.security import create_access_token  # noqa: E402
from app.services.accounts import register_user  # noqa: E402


# --------------------------------------------------------------------------
# A minimal in-memory Redis stand-in (only what the infra helpers call).
# --------------------------------------------------------------------------
class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, nx=False, px=None, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    def eval(self, *_args, **_kwargs):
        return 1

    def ping(self):
        return True


# --------------------------------------------------------------------------
# DB lifecycle — fresh schema per test.
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as c:  # context manager fires lifespan
        yield c


# --------------------------------------------------------------------------
# Factories.
# --------------------------------------------------------------------------
@pytest.fixture
def users(db):
    made = {"n": 0}

    def _make(role=ROLE_PATIENT, full_name=None, password="password123"):
        made["n"] += 1
        email = f"{role}{made['n']}@t.test"
        return register_user(
            db, email=email, password=password, full_name=full_name or f"{role} {made['n']}", role=role
        )

    return _make


@pytest.fixture
def make_doctor(db, users):
    def _make(specialisation="Cardiology", slot_duration_min=30, with_hours=True):
        doc_user = users(role=ROLE_DOCTOR, full_name="Dr. Test")
        profile = DoctorProfile(
            user_id=doc_user.id,
            specialisation=specialisation,
            slot_duration_min=slot_duration_min,
            active=True,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        if with_hours:
            for day in range(0, 7):
                db.add(
                    DoctorWorkingHours(
                        doctor_id=profile.id,
                        day_of_week=day,
                        start_time=dt.time(9, 0),
                        end_time=dt.time(17, 0),
                    )
                )
            db.commit()
        return profile

    return _make


@pytest.fixture
def make_slot(db):
    def _make(doctor_id, when=None, status=SLOT_FREE, minutes=30):
        when = when or (utcnow() + dt.timedelta(days=2)).replace(microsecond=0)
        slot = Slot(
            doctor_id=doctor_id,
            start_time=when,
            end_time=when + dt.timedelta(minutes=minutes),
            status=status,
        )
        db.add(slot)
        db.commit()
        db.refresh(slot)
        return slot

    return _make


@pytest.fixture
def auth():
    def _headers(user):
        return {"Authorization": f"Bearer {create_access_token(user)}"}

    return _headers


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def clinic_slot_time():
    """Return a factory → (clinic_local_date, utc_datetime) for a future slot.

    Slots are stored in UTC but ``apply_leave`` / availability reason about the
    *clinic-local* calendar day, so tests need both representations aligned.
    """
    tz = ZoneInfo(settings.CLINIC_TZ)

    def _make(days_ahead: int = 2, hour: int = 10, minute: int = 0):
        base_date = (utcnow().astimezone(tz) + dt.timedelta(days=days_ahead)).date()
        local = dt.datetime.combine(base_date, dt.time(hour, minute), tzinfo=tz)
        return base_date, local.astimezone(dt.timezone.utc)

    return _make
