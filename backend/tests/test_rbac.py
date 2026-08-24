"""(f) Role-based access control — admin routes reject patients (403) and the
unauthenticated (401), and admit admins (200)."""
from __future__ import annotations

from app.models import ROLE_ADMIN


def test_patient_forbidden_on_admin_route(client, db, users, auth):
    patient = users()
    resp = client.get("/api/admin/doctors", headers=auth(patient))
    assert resp.status_code == 403, resp.text


def test_unauthenticated_rejected_on_admin_route(client):
    resp = client.get("/api/admin/doctors")
    assert resp.status_code == 401, resp.text


def test_admin_allowed_on_admin_route(client, db, users, auth):
    admin = users(role=ROLE_ADMIN)
    resp = client.get("/api/admin/doctors", headers=auth(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
