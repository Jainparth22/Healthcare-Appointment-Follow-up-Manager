"""Doctor visit-completion flow.

Regression guard for a real bug: ``/api/visits/schedule`` exposes the primary
key as ``appointment_id`` while ``/api/appointments/mine`` uses ``id``. The
frontend read ``id`` off the schedule rows, got ``undefined``, and posted to
``/api/visits/appointments/undefined/complete`` — which FastAPI rejected with
"Input should be a valid integer" on the *path* param. These tests pin the
response key and drive completion using only the id the schedule hands out.
"""
from __future__ import annotations

import datetime as dt

from app.database import SessionLocal
from app.models import (
    APPT_COMPLETED,
    APPT_CONFIRMED,
    SLOT_BOOKED,
    Appointment,
    Slot,
)


def _confirmed_appointment(db, doctor, patient):
    when = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(microsecond=0)
    slot = Slot(
        doctor_id=doctor.id, start_time=when,
        end_time=when + dt.timedelta(minutes=30), status=SLOT_BOOKED,
    )
    db.add(slot)
    db.commit()
    db.refresh(slot)
    appt = Appointment(
        slot_id=slot.id, doctor_id=doctor.id, patient_id=patient.id,
        status=APPT_CONFIRMED, scheduled_start=when,
        scheduled_end=when + dt.timedelta(minutes=30), symptoms="tired and low energy",
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return appt


def test_schedule_exposes_appointment_id_key(client, db, users, make_doctor, auth):
    """The key the frontend reads must exist — renaming it breaks completion."""
    doctor = make_doctor()
    patient = users()
    _confirmed_appointment(db, doctor, patient)

    rows = client.get("/api/visits/schedule", headers=auth(doctor.user)).json()
    assert len(rows) == 1
    assert "appointment_id" in rows[0], "frontend reads row.appointment_id"
    assert isinstance(rows[0]["appointment_id"], int)


def test_complete_visit_using_id_from_schedule(client, db, users, make_doctor, auth):
    """Full flow with the id exactly as the schedule endpoint hands it out."""
    doctor = make_doctor()
    patient = users()
    _confirmed_appointment(db, doctor, patient)
    headers = auth(doctor.user)

    rows = client.get("/api/visits/schedule", headers=headers).json()
    appt_id = rows[0]["appointment_id"]

    # The payload the doctor form builds: free-text frequency, integer days.
    r = client.post(
        f"/api/visits/appointments/{appt_id}/complete",
        json={
            "doctor_notes": "Grief-related fatigue. Advised rest and follow-up in two weeks.",
            "prescriptions": [{
                "medication_name": "magnesium",
                "dosage": "200 mg",
                "frequency": "twice daily",
                "duration_days": 4,
            }],
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == APPT_COMPLETED
    assert body["postvisit"]["summary_text"]
    assert body["source"] in ("ok", "fallback")

    # "twice daily" is parsed server-side into times_per_day=2, so 4 days give
    # up to 8 doses — minus any dose whose time today has already passed.
    presc = body["prescriptions"][0]
    assert presc["duration_days"] == 4
    assert presc["frequency"] == "twice daily"
    assert 0 < presc["reminders_scheduled"] <= 8

    with SessionLocal() as s:
        assert s.get(Appointment, appt_id).status == APPT_COMPLETED


def test_complete_visit_rejects_non_numeric_appointment_id(client, db, users, make_doctor, auth):
    """The exact failure the stale frontend produced: 'undefined' in the path."""
    doctor = make_doctor()
    r = client.post(
        "/api/visits/appointments/undefined/complete",
        json={"doctor_notes": "x", "prescriptions": []},
        headers=auth(doctor.user),
    )
    assert r.status_code == 422
    assert any(e["loc"][:2] == ["path", "appointment_id"] for e in r.json()["detail"])
