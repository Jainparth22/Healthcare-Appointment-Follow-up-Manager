"""(c) Availability correctness — free listing excludes blocked/booked slots and
is stable across repeated calls (the cache-aside contract; with Redis down the
producer runs each time but must return identical data)."""
from __future__ import annotations

from app.models import SLOT_BLOCKED, SLOT_BOOKED, SLOT_FREE
from app.services import booking


def test_get_free_slots_excludes_non_free(db, make_doctor, make_slot, clinic_slot_time):
    doctor = make_doctor(with_hours=False)  # no auto-generation; we control inventory
    d, free_at = clinic_slot_time(hour=10)
    _, booked_at = clinic_slot_time(hour=11)
    _, blocked_at = clinic_slot_time(hour=12)

    free = make_slot(doctor.id, when=free_at, status=SLOT_FREE)
    make_slot(doctor.id, when=booked_at, status=SLOT_BOOKED)
    make_slot(doctor.id, when=blocked_at, status=SLOT_BLOCKED)

    result = booking.get_free_slots(db, doctor.id, d)
    assert [s.id for s in result] == [free.id]


def test_available_payload_is_stable(db, make_doctor, make_slot, clinic_slot_time):
    doctor = make_doctor(with_hours=False)
    d, free_at = clinic_slot_time(hour=10)
    make_slot(doctor.id, when=free_at, status=SLOT_FREE)
    make_slot(doctor.id, when=clinic_slot_time(hour=13)[1], status=SLOT_BOOKED)

    first = booking.available_slots_payload(db, doctor.id, d.isoformat())
    second = booking.available_slots_payload(db, doctor.id, d.isoformat())

    assert len(first) == 1
    assert first == second  # deterministic / cache-consistent
    assert first[0]["status"] == SLOT_FREE
