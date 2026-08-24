"""End-to-end journey harness — drives the app the way the BROWSER does.

Unlike the pytest suite (which uses Bearer tokens), this logs in through the
HTML form, keeps the session cookie, and echoes the `hcv_csrf` cookie in the
X-CSRF-Token header — exactly what app.js does. That exercises the cookie +
CSRF path and every JSON contract the real frontend depends on.

Run:  PYTHONPATH=. python tools/e2e_journey.py
"""
from __future__ import annotations

import os
import pathlib
import tempfile

_TMP = tempfile.mkdtemp(prefix="hcv-e2e-")
os.environ.update(
    DATABASE_URL=f"sqlite:///{(pathlib.Path(_TMP) / 'e2e.db').as_posix()}",
    CELERY_TASK_ALWAYS_EAGER="true",
    EMAIL_BACKEND="console",
    GEMINI_API_KEY="",
    ANTHROPIC_API_KEY="",
    SECRET_KEY="e2e-secret-key",
    RATE_LIMIT_ENABLED="false",
    REDIS_URL="redis://127.0.0.1:6399/15",  # unreachable -> degraded path
)

import datetime as dt  # noqa: E402
import logging  # noqa: E402

logging.disable(logging.CRITICAL)  # silence app access logs; we print our own

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ROLE_ADMIN, ROLE_PATIENT  # noqa: E402
from app.services.accounts import register_user  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((("PASS" if ok else "FAIL"), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


class Browser:
    """A TestClient that behaves like app.js: cookies + X-CSRF-Token."""

    def __init__(self, client: TestClient):
        self.c = client

    def _csrf(self) -> dict:
        tok = self.c.cookies.get("hcv_csrf")
        return {"X-CSRF-Token": tok} if tok else {}

    def login(self, email: str, password: str):
        return self.c.post(
            "/login", data={"email": email, "password": password}, follow_redirects=False
        )

    def get(self, url: str, **kw):
        return self.c.get(url, **kw)

    def post(self, url: str, json=None, extra_headers=None):
        h = self._csrf()
        if extra_headers:
            h.update(extra_headers)
        return self.c.post(url, json=json, headers=h)

    def put(self, url: str, json=None):
        return self.c.put(url, json=json, headers=self._csrf())

    def delete(self, url: str):
        return self.c.delete(url, headers=self._csrf())

    def logout(self):
        return self.c.post("/logout", data={}, follow_redirects=False)


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    # Seed only the accounts; everything else goes through the real UI/API.
    with SessionLocal() as db:
        register_user(db, email="admin@e2e.test", password="admin12345",
                      full_name="Admin", role=ROLE_ADMIN)
        register_user(db, email="pat@e2e.test", password="patient12345",
                      full_name="Test Patient", role=ROLE_PATIENT)
        # A second patient, so slot contention can be tested across *different*
        # users. Same-user re-holds are idempotent by design, not a conflict.
        register_user(db, email="rival@e2e.test", password="patient12345",
                      full_name="Rival Patient", role=ROLE_PATIENT)

    with TestClient(app) as client:
        b = Browser(client)

        print("\n== Health ==")
        check("GET /healthz", b.get("/healthz").status_code == 200)
        r = b.get("/readyz")
        check("GET /readyz tolerates Redis down", r.status_code == 200,
              f"status={r.status_code} body={r.json()}")

        print("\n== Public pages render ==")
        check("GET / (login page)", b.get("/").status_code == 200)

        # ---------------- ADMIN journey ----------------
        print("\n== Admin journey ==")
        r = b.login("admin@e2e.test", "admin12345")
        check("admin form login sets cookies", r.status_code == 303 and
              bool(client.cookies.get("hcv_csrf")), f"status={r.status_code}")
        check("GET /admin page", b.get("/admin").status_code == 200)

        doctor_payload = {
            "full_name": "Dr. E2E", "email": "dr@e2e.test", "password": "doctor12345",
            "specialisation": "Cardiology", "bio": "Heart specialist",
            "slot_duration_min": 30, "phone": "+10000000000",
            "working_hours": [{"day_of_week": d, "start_time": "09:00", "end_time": "17:00"}
                              for d in range(7)],
        }
        r = b.post("/api/admin/doctors", json=doctor_payload)
        ok = r.status_code in (200, 201)
        check("POST /api/admin/doctors (create doctor)", ok, "" if ok else r.text[:300])
        if not ok:
            return summarise()
        doc = r.json()
        doctor_id = doc.get("id")
        check("created doctor response has 'id'", doctor_id is not None, f"keys={sorted(doc)}")

        r = b.get("/api/admin/doctors")
        check("GET /api/admin/doctors lists", r.status_code == 200 and len(r.json()) >= 1)

        r = b.get(f"/api/admin/doctors/{doctor_id}")
        detail_ok = r.status_code == 200
        check("GET /api/admin/doctors/{id} detail", detail_ok)
        if detail_ok:
            d = r.json()
            # app.js reads d.working_hours[].{day_of_week,start_time,end_time} and d.leaves[]
            check("detail has working_hours + leaves (app.js reads both)",
                  "working_hours" in d and "leaves" in d, f"keys={sorted(d)}")
            if d.get("working_hours"):
                w = d["working_hours"][0]
                check("working_hours row has day_of_week/start_time/end_time",
                      {"day_of_week", "start_time", "end_time"} <= set(w), f"keys={sorted(w)}")
                # app.js does w.start_time.slice(0,5) -> must be a string
                check("working_hours.start_time is a string (app.js .slice)",
                      isinstance(w.get("start_time"), str), f"type={type(w.get('start_time')).__name__}")

        r = b.put(f"/api/admin/doctors/{doctor_id}/working-hours",
                  json=[{"day_of_week": d, "start_time": "09:00", "end_time": "17:00"}
                        for d in range(7)])
        check("PUT working-hours", r.status_code == 200, "" if r.status_code == 200 else r.text[:200])

        r = b.patch = client.patch(f"/api/admin/doctors/{doctor_id}",
                                   json={"bio": "Updated bio"}, headers=b._csrf())
        check("PATCH /api/admin/doctors/{id}", r.status_code == 200,
              "" if r.status_code == 200 else r.text[:200])

        r = b.get("/api/admin/outbox")
        check("GET /api/admin/outbox", r.status_code == 200)
        if r.status_code == 200 and r.json():
            o = r.json()[0]
            check("outbox row has keys app.js renders",
                  {"to_email", "subject", "kind", "status", "attempts", "max_attempts"} <= set(o),
                  f"keys={sorted(o)}")

        b.logout()

        # ---------------- PATIENT journey ----------------
        print("\n== Patient journey ==")
        r = b.login("pat@e2e.test", "patient12345")
        check("patient form login", r.status_code == 303)
        check("GET /patient page", b.get("/patient").status_code == 200)

        r = b.get("/api/doctors")
        docs_ok = r.status_code == 200 and len(r.json()) >= 1
        check("GET /api/doctors (search)", docs_ok, "" if docs_ok else r.text[:200])
        if docs_ok:
            dd = r.json()[0]
            check("doctor card fields (id/full_name/specialisation)",
                  {"id", "full_name", "specialisation"} <= set(dd), f"keys={sorted(dd)}")

        r = b.get("/api/doctors?specialisation=Cardio")
        check("GET /api/doctors filtered", r.status_code == 200 and len(r.json()) >= 1)

        r = b.get(f"/api/doctors/{doctor_id}")
        prof_ok = r.status_code == 200
        check("GET /api/doctors/{id}", prof_ok)
        if prof_ok:
            check("profile has slot_duration_min (book page reads it)",
                  "slot_duration_min" in r.json(), f"keys={sorted(r.json())}")

        check("GET /patient/book/{id} page", b.get(f"/patient/book/{doctor_id}").status_code == 200)

        # Slots for tomorrow (lazily materialised)
        target = (dt.datetime.now() + dt.timedelta(days=1)).date().isoformat()
        r = b.get(f"/api/doctors/{doctor_id}/slots?date={target}")
        slots_ok = r.status_code == 200 and len(r.json()) > 0
        check("GET /api/doctors/{id}/slots returns slots", slots_ok,
              f"status={r.status_code} n={len(r.json()) if r.status_code == 200 else '?'}")
        if not slots_ok:
            return summarise()
        slots = r.json()
        check("slot has id + start_time (app.js reads both)",
              {"id", "start_time"} <= set(slots[0]), f"keys={sorted(slots[0])}")

        # Two-phase booking: hold -> symptoms -> confirm
        r = b.post("/api/appointments/hold", json={"slot_id": slots[0]["id"]},
                   extra_headers={"Idempotency-Key": "e2e-hold-1"})
        hold_ok = r.status_code in (200, 201)
        check("POST /api/appointments/hold", hold_ok, "" if hold_ok else r.text[:300])
        if not hold_ok:
            return summarise()
        appt_id = r.json().get("id")
        check("hold response has 'id' (app.js reads r.data.id)", appt_id is not None,
              f"keys={sorted(r.json())}")

        # Idempotent replay must return the SAME appointment
        r2 = b.post("/api/appointments/hold", json={"slot_id": slots[0]["id"]},
                    extra_headers={"Idempotency-Key": "e2e-hold-1"})
        check("hold replay with same Idempotency-Key returns same appointment",
              r2.status_code in (200, 201) and r2.json().get("id") == appt_id,
              f"status={r2.status_code} id={r2.json().get('id')} vs {appt_id}")

        # Same patient, DIFFERENT key: still the same hold. This is the domain
        # -level idempotency that survives Redis being down (the replay cache
        # is Redis-only), so a double-click never says "slot was just taken".
        r3 = b.post("/api/appointments/hold", json={"slot_id": slots[0]["id"]},
                    extra_headers={"Idempotency-Key": "e2e-hold-DIFFERENT"})
        check("re-holding own slot is idempotent even without the replay cache",
              r3.status_code in (200, 201) and r3.json().get("id") == appt_id,
              f"status={r3.status_code} id={r3.json().get('id')} vs {appt_id}")

        # A DIFFERENT patient must lose — this is the double-booking guarantee.
        rival = Browser(TestClient(app))
        rival.login("rival@e2e.test", "patient12345")
        r4 = rival.post("/api/appointments/hold", json={"slot_id": slots[0]["id"]},
                        extra_headers={"Idempotency-Key": "rival-hold-1"})
        check("another patient cannot hold the same slot (409)", r4.status_code == 409,
              f"status={r4.status_code}")

        r = b.post(f"/api/appointments/{appt_id}/symptoms",
                   json={"symptoms": "Chest pain and shortness of breath for two days"})
        sym_ok = r.status_code == 200
        check("POST /{id}/symptoms -> pre-visit summary", sym_ok, "" if sym_ok else r.text[:300])
        if sym_ok:
            body = r.json()
            check("symptoms response has previsit + source", {"previsit", "source"} <= set(body),
                  f"keys={sorted(body)}")
            pv = body.get("previsit", {})
            check("previsit has urgency_level/chief_complaint/suggested_questions",
                  {"urgency_level", "chief_complaint", "suggested_questions"} <= set(pv),
                  f"keys={sorted(pv)}")
            check("urgency_level is Low/Medium/High (CSS class depends on it)",
                  pv.get("urgency_level") in ("Low", "Medium", "High"), f"got={pv.get('urgency_level')}")
            check("LLM degrades to fallback with no API key", body.get("source") == "fallback",
                  f"source={body.get('source')}")

        r = b.post(f"/api/appointments/{appt_id}/confirm",
                   extra_headers={"Idempotency-Key": "e2e-confirm-1"})
        conf_ok = r.status_code == 200
        check("POST /{id}/confirm", conf_ok, "" if conf_ok else r.text[:300])

        r = b.post(f"/api/appointments/{appt_id}/confirm",
                   extra_headers={"Idempotency-Key": "e2e-confirm-1"})
        check("confirm replay is idempotent (not an error)", r.status_code == 200,
              f"status={r.status_code}")

        r = b.get("/api/appointments/mine")
        mine_ok = r.status_code == 200 and len(r.json()) >= 1
        check("GET /api/appointments/mine", mine_ok)
        if mine_ok:
            a = r.json()[0]
            check("mine row has id/status/doctor_name/scheduled_start (apptCard reads these)",
                  {"id", "status", "doctor_name", "scheduled_start"} <= set(a), f"keys={sorted(a)}")
            check("mine row exposes previsit for the card", "previsit" in a, f"keys={sorted(a)}")

        # CSRF must be enforced on cookie-auth writes
        r = client.post("/api/appointments/hold", json={"slot_id": slots[1]["id"]})
        check("write without X-CSRF-Token is rejected", r.status_code in (401, 403),
              f"status={r.status_code}")

        # RBAC: patient must not reach admin routes
        r = b.get("/api/admin/doctors")
        check("patient blocked from admin route (403)", r.status_code == 403, f"status={r.status_code}")

        r = b.get("/api/visits/schedule")
        check("patient blocked from doctor route (403)", r.status_code == 403, f"status={r.status_code}")

        # Role-based page redirect
        r = client.get("/admin", follow_redirects=False)
        check("patient visiting /admin is redirected", r.status_code == 303, f"status={r.status_code}")

        r = b.get("/api/integrations/google/status")
        gs_ok = r.status_code == 200
        check("GET google/status", gs_ok)
        if gs_ok:
            check("google status has configured+connected (app.js destructures both)",
                  {"configured", "connected"} <= set(r.json()), f"keys={sorted(r.json())}")

        b.logout()

        # ---------------- DOCTOR journey ----------------
        print("\n== Doctor journey ==")
        r = b.login("dr@e2e.test", "doctor12345")
        check("doctor form login", r.status_code == 303, f"status={r.status_code}")
        check("GET /doctor page", b.get("/doctor").status_code == 200)

        r = b.get("/api/visits/schedule")
        sch_ok = r.status_code == 200 and len(r.json()) >= 1
        check("GET /api/visits/schedule", sch_ok,
              f"status={r.status_code} n={len(r.json()) if r.status_code == 200 else '?'}")
        if not sch_ok:
            return summarise()
        row = r.json()[0]
        check("schedule row has appointment_id (the bug we fixed)",
              "appointment_id" in row, f"keys={sorted(row)}")
        check("schedule row has patient_name/status/scheduled_start",
              {"patient_name", "status", "scheduled_start"} <= set(row), f"keys={sorted(row)}")
        sched_id = row["appointment_id"]

        r = b.get(f"/api/visits/appointments/{sched_id}/previsit")
        check("GET /{id}/previsit", r.status_code == 200, "" if r.status_code == 200 else r.text[:200])

        r = b.post(f"/api/visits/appointments/{sched_id}/complete", json={
            "doctor_notes": "Stable angina suspected. Advised rest, ECG follow-up in one week.",
            "prescriptions": [
                {"medication_name": "Magnesium", "dosage": "200 mg",
                 "frequency": "twice daily", "duration_days": 4},
                {"medication_name": "Aspirin", "dosage": "75 mg",
                 "frequency": "once daily", "duration_days": 7},
            ],
        })
        comp_ok = r.status_code == 200
        check("POST /{id}/complete (the reported bug)", comp_ok, "" if comp_ok else r.text[:300])
        if comp_ok:
            body = r.json()
            check("complete response has postvisit/source/prescriptions",
                  {"postvisit", "source", "prescriptions"} <= set(body), f"keys={sorted(body)}")
            pv = body["postvisit"]
            check("postvisit has summary_text/medication_schedule/follow_up_steps",
                  {"summary_text", "medication_schedule", "follow_up_steps"} <= set(pv),
                  f"keys={sorted(pv)}")
            ps = body["prescriptions"]
            check("both prescriptions saved", len(ps) == 2, f"n={len(ps)}")
            check("prescription rows expose reminders_scheduled (app.js reduces on it)",
                  all("reminders_scheduled" in p for p in ps), f"keys={sorted(ps[0])}")
            check("medication reminders were actually scheduled",
                  sum(p["reminders_scheduled"] for p in ps) > 0,
                  f"total={sum(p['reminders_scheduled'] for p in ps)}")

        r = b.post(f"/api/visits/appointments/{sched_id}/complete",
                   json={"doctor_notes": "again", "prescriptions": []})
        check("completing twice is rejected (409)", r.status_code == 409, f"status={r.status_code}")

        b.logout()

        # ---------------- Cancel + reschedule ----------------
        print("\n== Cancel / reschedule ==")
        b.login("pat@e2e.test", "patient12345")
        r = b.get(f"/api/doctors/{doctor_id}/slots?date={target}")
        free = r.json()
        r = b.post("/api/appointments/hold", json={"slot_id": free[0]["id"]},
                   extra_headers={"Idempotency-Key": "e2e-hold-2"})
        if r.status_code in (200, 201):
            aid2 = r.json()["id"]
            b.post(f"/api/appointments/{aid2}/symptoms", json={"symptoms": "mild headache"})
            b.post(f"/api/appointments/{aid2}/confirm", extra_headers={"Idempotency-Key": "e2e-c2"})
            r = b.post(f"/api/appointments/{aid2}/reschedule", json={"new_slot_id": free[1]["id"]})
            check("POST /{id}/reschedule", r.status_code == 200,
                  "" if r.status_code == 200 else r.text[:300])
            r = b.post(f"/api/appointments/{aid2}/cancel")
            check("POST /{id}/cancel", r.status_code == 200,
                  "" if r.status_code == 200 else r.text[:300])
        else:
            check("second hold for reschedule test", False, r.text[:200])

        # ---------------- Doctor leave cascade ----------------
        print("\n== Admin leave cascade ==")
        b.logout()
        b.login("admin@e2e.test", "admin12345")
        r = b.post(f"/api/admin/doctors/{doctor_id}/leave",
                   json={"leave_date": target, "reason": "Conference"})
        leave_ok = r.status_code in (200, 201)
        check("POST /{id}/leave", leave_ok, "" if leave_ok else r.text[:300])
        if leave_ok:
            check("leave response has cancelled_appointments (app.js reads it)",
                  "cancelled_appointments" in r.json(), f"keys={sorted(r.json())}")
        r = b.get(f"/api/doctors/{doctor_id}/slots?date={target}")
        check("slots empty on a leave day", r.status_code == 200 and len(r.json()) == 0,
              f"n={len(r.json()) if r.status_code == 200 else '?'}")
        r = b.delete(f"/api/admin/doctors/{doctor_id}/leave/{target}")
        check("DELETE leave", r.status_code in (200, 204), f"status={r.status_code}")

    return summarise()


def summarise() -> int:
    failed = [r for r in RESULTS if r[0] == "FAIL"]
    print("\n" + "=" * 72)
    print(f"TOTAL {len(RESULTS)}   PASS {len(RESULTS) - len(failed)}   FAIL {len(failed)}")
    if failed:
        print("\nFAILURES:")
        for _, name, detail in failed:
            print(f"  - {name}" + (f"\n      {detail}" if detail else ""))
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
