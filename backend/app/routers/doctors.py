"""Doctor discovery (JSON API): search by specialisation, profile + working
hours, and cached available slots. Readable by any authenticated user.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..errors import BadRequest, NotFound
from ..infra.cache import cached_json, doctor_version, search_version
from ..models import DoctorProfile, User
from ..schemas import DoctorOut, SlotOut
from ..security import get_current_user
from ..services import booking

router = APIRouter(prefix="/api/doctors", tags=["doctors"])


def _doctor_out(d: DoctorProfile) -> dict:
    return {
        "id": d.id,
        "full_name": d.user.full_name,
        "specialisation": d.specialisation,
        "bio": d.bio,
        "slot_duration_min": d.slot_duration_min,
        "active": d.active,
    }


@router.get("", response_model=list[DoctorOut])
def search_doctors(
    specialisation: str | None = Query(default=None, max_length=120),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ver = search_version()
    key = f"cache:doctors:{(specialisation or '').strip().lower()}:{ver}"

    def producer() -> list[dict]:
        stmt = select(DoctorProfile).join(User).where(DoctorProfile.active.is_(True))
        if specialisation:
            stmt = stmt.where(DoctorProfile.specialisation.ilike(f"%{specialisation}%"))
        stmt = stmt.order_by(DoctorProfile.specialisation)
        return [_doctor_out(d) for d in db.scalars(stmt).all()]

    return cached_json(key, ttl=30, producer=producer)


@router.get("/{doctor_id}")
def doctor_profile(
    doctor_id: int,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ver = doctor_version(doctor_id)
    key = f"cache:doctor:{doctor_id}:{ver}"

    def producer() -> dict:
        d = db.get(DoctorProfile, doctor_id)
        if d is None:
            raise NotFound("Doctor not found")
        hours = sorted(d.working_hours, key=lambda w: (w.day_of_week, w.start_time))
        return {
            **_doctor_out(d),
            "working_hours": [
                {
                    "day_of_week": w.day_of_week,
                    "start_time": w.start_time.isoformat(),
                    "end_time": w.end_time.isoformat(),
                }
                for w in hours
            ],
        }

    return cached_json(key, ttl=60, producer=producer)


@router.get("/{doctor_id}/slots", response_model=list[SlotOut])
def doctor_slots(
    doctor_id: int,
    date: str | None = Query(default=None, description="YYYY-MM-DD (clinic timezone)"),
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if date is None:
        date = dt.datetime.now(ZoneInfo(settings.CLINIC_TZ)).date().isoformat()
    else:
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            raise BadRequest("Invalid date; expected YYYY-MM-DD")
    return booking.available_slots_payload(db, doctor_id, date)
