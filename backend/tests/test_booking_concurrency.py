"""(a) Double-booking prevention — the core correctness guarantee.

Two holds on the same slot must resolve to exactly one winner. We test both the
deterministic path (sequential) and a threaded stress variant that genuinely
races two independent DB sessions through the L2 compare-and-swap. Redis is down
in tests, so the Redis lock (L1) fails open and the CAS is the sole guarantee —
exactly the property we want to prove.
"""
from __future__ import annotations

import threading

import pytest

from app.database import SessionLocal
from app.errors import Conflict
from app.models import APPT_HOLDING, SLOT_HELD, Appointment, Slot
from app.services import booking


def test_sequential_double_hold_one_conflict(db, make_doctor, users, make_slot, clinic_slot_time):
    doctor = make_doctor(with_hours=False)
    _, when = clinic_slot_time()
    slot = make_slot(doctor.id, when=when)
    p1 = users()
    p2 = users()

    appt = booking.hold_slot(db, p1.id, slot.id)
    assert appt.status == APPT_HOLDING

    with pytest.raises(Conflict):
        booking.hold_slot(db, p2.id, slot.id)

    db.expire_all()
    reloaded = db.get(Slot, slot.id)
    assert reloaded.status == SLOT_HELD
    assert reloaded.held_by == p1.id


def test_concurrent_double_hold_stress(db, make_doctor, users, make_slot, clinic_slot_time):
    """Two threads race the same slot; exactly one may win."""
    doctor = make_doctor(with_hours=False)
    _, when = clinic_slot_time()
    slot = make_slot(doctor.id, when=when)
    slot_id = slot.id
    patients = [users().id, users().id]

    results: list[tuple[str, object]] = []
    barrier = threading.Barrier(len(patients))
    lock = threading.Lock()

    def worker(patient_id: int):
        session = SessionLocal()
        try:
            barrier.wait()  # maximise the overlap on the CAS
            appt = booking.hold_slot(session, patient_id, slot_id)
            with lock:
                results.append(("ok", appt.id))
        except Exception as exc:  # noqa: BLE001 - loser is expected to raise
            with lock:
                results.append(("err", exc))
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(pid,)) for pid in patients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wins = [r for r in results if r[0] == "ok"]
    assert len(wins) == 1, f"expected exactly one winner, got {results}"

    # The invariant that actually matters: the DB holds one held slot and one
    # holding appointment — no double-booking regardless of thread timing.
    check = SessionLocal()
    try:
        s = check.get(Slot, slot_id)
        assert s.status == SLOT_HELD
        assert s.held_by in patients
        holding = check.query(Appointment).filter(
            Appointment.slot_id == slot_id, Appointment.status == APPT_HOLDING
        ).all()
        assert len(holding) == 1
    finally:
        check.close()
