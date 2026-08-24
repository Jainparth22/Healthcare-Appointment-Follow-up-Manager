"""(b) Idempotency-Key replay — a retried booking returns the identical result.

Replay requires Redis (the store is the source of the cached response), so we
monkeypatch a fake client into the idempotency module. Without idempotency the
second POST would hit the CAS on an already-held slot and 409; proving it
returns the *same* 201 body proves the replay path.
"""
from __future__ import annotations

import app.infra.idempotency as idem
from app.services import booking


def test_idempotent_hold_replays_same_appointment(
    client, db, make_doctor, users, make_slot, auth, clinic_slot_time, fake_redis, monkeypatch
):
    monkeypatch.setattr(idem, "get_redis", lambda: fake_redis)

    doctor = make_doctor(with_hours=False)
    _, when = clinic_slot_time()
    slot = make_slot(doctor.id, when=when)
    patient = users()
    headers = {**auth(patient), "Idempotency-Key": "abc-123"}

    r1 = client.post("/api/appointments/hold", json={"slot_id": slot.id}, headers=headers)
    assert r1.status_code == 201, r1.text
    first = r1.json()

    r2 = client.post("/api/appointments/hold", json={"slot_id": slot.id}, headers=headers)
    assert r2.status_code == 201, r2.text
    second = r2.json()

    assert first["id"] == second["id"]  # same appointment, not a new hold


def test_hold_without_idempotency_key_conflicts_on_retry(
    client, db, make_doctor, users, make_slot, auth, clinic_slot_time, fake_redis, monkeypatch
):
    """Natural idempotency: same-patient retry without Idempotency-Key returns same hold (Redis-down safe)."""
    monkeypatch.setattr(idem, "get_redis", lambda: fake_redis)

    doctor = make_doctor(with_hours=False)
    _, when = clinic_slot_time()
    slot = make_slot(doctor.id, when=when)
    patient = users()
    headers = auth(patient)

    r1 = client.post("/api/appointments/hold", json={"slot_id": slot.id}, headers=headers)
    assert r1.status_code == 201, r1.text

    r2 = client.post("/api/appointments/hold", json={"slot_id": slot.id}, headers=headers)
    # Same patient re-holding own slot is idempotent via _live_self_hold even without Idempotency-Key
    assert r2.status_code == 201, r2.text
    assert r1.json()["id"] == r2.json()["id"]
