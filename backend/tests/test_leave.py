"""(d) Doctor leave — marking a leave day blocks the day's slots, cancels the
affected appointments, and enqueues cancellation emails to those patients.

Driven through the admin HTTP route so it also exercises admin RBAC, the
CSRF-exempt Bearer path, and the booking→notifications wiring."""
from __future__ import annotations

from app.models import (
    APPT_CANCELLED,
    ROLE_ADMIN,
    SLOT_BLOCKED,
    Appointment,
    EmailOutbox,
    Slot,
)
from app.services import booking


def test_leave_cancels_and_notifies(
    client, db, make_doctor, users, make_slot, auth, clinic_slot_time
):
    doctor = make_doctor(with_hours=False)
    patient = users()
    leave_date, when = clinic_slot_time(hour=10)
    slot = make_slot(doctor.id, when=when)

    # Patient books and confirms the slot on the day the doctor will take leave.
    appt = booking.hold_slot(db, patient.id, slot.id)
    booking.confirm_appointment(db, patient.id, appt.id)
    appt_id, slot_id = appt.id, slot.id

    admin = users(role=ROLE_ADMIN)
    resp = client.post(
        f"/api/admin/doctors/{doctor.id}/leave",
        json={"leave_date": leave_date.isoformat(), "reason": "conference"},
        headers=auth(admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cancelled_appointments"] == 1

    db.expire_all()
    assert db.get(Appointment, appt_id).status == APPT_CANCELLED
    assert db.get(Slot, slot_id).status == SLOT_BLOCKED

    mail = db.query(EmailOutbox).filter(EmailOutbox.related_appointment_id == appt_id).all()
    # Now notifies BOTH patient and doctor (critical fix for PDF requirement)
    assert len(mail) == 2, f"expected patient+doctor emails, got {[(m.to_email, m.kind) for m in mail]}"
    kinds = {m.kind for m in mail}
    assert "cancellation" in kinds
    assert "cancellation_doctor" in kinds
    patient_mail = [m for m in mail if m.to_email == patient.email][0]
    assert patient_mail.kind == "cancellation"
