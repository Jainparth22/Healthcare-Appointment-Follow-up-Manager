"""Pydantic request/response schemas."""
from __future__ import annotations

import datetime as dt
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import ROLE_PATIENT, ROLES
from .validators import Email

# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
class RegisterIn(BaseModel):
    email: Email
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=40)
    role: str = ROLE_PATIENT  # only patient self-registration is honoured server-side

    @field_validator("role")
    @classmethod
    def _valid_role(cls, v: str) -> str:
        return v if v in ROLES else ROLE_PATIENT


class LoginIn(BaseModel):
    email: Email
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    # Deliberately a plain `str`, not a validated Email: this is an OUTPUT model.
    # Re-validating an address already persisted in the database turns a data
    # problem into a 500 for the whole endpoint (that is exactly what happened
    # to GET /api/auth/me for every `@clinic.test` demo account). Validation
    # belongs on the way IN.
    email: str
    full_name: str
    role: str
    phone: Optional[str] = None


# --------------------------------------------------------------------------
# Doctors / admin
# --------------------------------------------------------------------------
class WorkingHourIn(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    start_time: dt.time
    end_time: dt.time

    @field_validator("end_time")
    @classmethod
    def _end_after_start(cls, v: dt.time, info):
        start = info.data.get("start_time")
        if start is not None and v <= start:
            raise ValueError("end_time must be after start_time")
        return v


class DoctorCreateIn(BaseModel):
    email: Email
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    specialisation: str = Field(min_length=1, max_length=120)
    bio: Optional[str] = None
    slot_duration_min: int = Field(default=30, ge=5, le=240)
    phone: Optional[str] = Field(default=None, max_length=40)
    working_hours: list[WorkingHourIn] = Field(default_factory=list)


class DoctorUpdateIn(BaseModel):
    full_name: Optional[str] = Field(default=None, max_length=200)
    specialisation: Optional[str] = Field(default=None, max_length=120)
    bio: Optional[str] = None
    slot_duration_min: Optional[int] = Field(default=None, ge=5, le=240)
    active: Optional[bool] = None


class DoctorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    full_name: str
    specialisation: str
    bio: Optional[str] = None
    slot_duration_min: int
    active: bool


class LeaveIn(BaseModel):
    leave_date: dt.date
    reason: Optional[str] = Field(default=None, max_length=255)


class SlotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    doctor_id: int
    start_time: dt.datetime
    end_time: dt.datetime
    status: str


# --------------------------------------------------------------------------
# Appointments (patient flow)
# --------------------------------------------------------------------------
class HoldIn(BaseModel):
    slot_id: int


class SymptomsIn(BaseModel):
    symptoms: str = Field(min_length=1, max_length=4000)


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    slot_id: Optional[int] = None
    doctor_id: int
    patient_id: int
    status: str
    symptoms: Optional[str] = None
    scheduled_start: Optional[dt.datetime] = None
    created_at: dt.datetime


class RescheduleIn(BaseModel):
    new_slot_id: int


# --------------------------------------------------------------------------
# LLM structured outputs
# --------------------------------------------------------------------------
UrgencyLevel = Literal["Low", "Medium", "High"]


class PreVisitSummary(BaseModel):
    """Structured pre-visit summary — the LLM is constrained to this shape."""
    urgency_level: UrgencyLevel
    chief_complaint: str
    suggested_questions: list[str] = Field(min_length=1)

    @field_validator("suggested_questions")
    @classmethod
    def _cap_questions(cls, v: list[str]) -> list[str]:
        # Spec asks for three; be lenient on input but normalise to three.
        cleaned = [q.strip() for q in v if q and q.strip()]
        if not cleaned:
            cleaned = ["What should I expect during this visit?"]
        return cleaned[:3]


class PostVisitSummary(BaseModel):
    summary_text: str
    medication_schedule: list[str] = Field(default_factory=list)
    follow_up_steps: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Visits (doctor flow)
# --------------------------------------------------------------------------
class PrescriptionIn(BaseModel):
    medication_name: str = Field(min_length=1, max_length=200)
    dosage: Optional[str] = Field(default=None, max_length=120)
    frequency: Optional[str] = Field(default=None, max_length=120)
    duration_days: int = Field(default=1, ge=1, le=365)
    instructions: Optional[str] = None


class CompleteVisitIn(BaseModel):
    doctor_notes: str = Field(min_length=1, max_length=8000)
    prescriptions: list[PrescriptionIn] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Generic
# --------------------------------------------------------------------------
class Message(BaseModel):
    detail: str
