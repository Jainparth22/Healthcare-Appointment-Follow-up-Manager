## Live Deploy Link - https://hcv-parth.up.railway.app/

# Healthcare Appointment & Follow-up Manager

A clinic platform with **patient / doctor / admin** portals. Patients book
appointments, submit symptoms (→ AI pre-visit summary with urgency), and receive
a patient-friendly post-visit summary plus medication reminders. Doctors manage
their schedule and record visit notes + prescriptions. Admins manage doctor
profiles, working hours, and leave. The system prevents double-booking, handles
simultaneous bookings safely, notifies patients when a doctor goes on leave, and
degrades gracefully when the LLM, email, calendar, or Redis are unavailable.

- **Backend:** FastAPI + Uvicorn (JSON API + Jinja2/HTMX pages in one service)
- **Data:** SQLAlchemy 2.0 + Alembic, single `DATABASE_URL` switch (SQLite dev/tests · Postgres prod)
- **Async:** Redis + Celery (worker + Beat) for email, calendar, reminders, hold-expiry, slot generation
- **AI:** Google Gemini behind a pluggable provider interface, always with a deterministic fallback

> **Design docs:** [DESIGN.md](DESIGN.md) (≤800-word write-up of the four hard
> problems) · [ARCHITECTURE.md](ARCHITECTURE.md) (full deep-dive: diagrams,
> concurrency proof, failure modes, scaling, security, trade-offs).

---

## Quickstart — zero external infrastructure

No Redis, no Postgres, no API keys required. Uses SQLite, runs Celery tasks
inline, prints email to the console, and uses the LLM fallback.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head        # or skip — SQLite auto-creates tables on startup
python -m app.seed          # demo admin + doctor + patient, and a bookable slot window
uvicorn app.main:app --reload
```

Open:

- **App:** http://localhost:8000/ (login / register)
- **API docs (Swagger):** http://localhost:8000/docs
- **Readiness:** http://localhost:8000/readyz

### Demo accounts (from `python -m app.seed`)

| Role    | Email                | Password      |
|---------|----------------------|---------------|
| Admin   | `admin@clinic.test`  | `admin12345`  |
| Doctor  | `dr.rao@clinic.test` | `doctor12345` |
| Patient | `patient@clinic.test`| `patient12345`|

### Try the end-to-end flow

1. Log in as the **patient** → search doctors → pick a slot → **hold** →
   enter symptoms (see the AI pre-visit summary + urgency) → **confirm**.
   A booking-confirmation email prints to the console; an outbox row is created.
2. Log in as the **doctor** → open the visit → add notes + a prescription →
   **complete** (generates the patient-friendly post-visit summary and schedules
   medication reminders).
3. Log in as the **admin** → mark the doctor on **leave** for the booked day →
   the affected patient's appointment is cancelled and a cancellation email is
   enqueued.

---

## Full topology with Docker Compose

Brings up **Postgres + Redis + web + Celery worker + Celery Beat**. The web
container runs `alembic upgrade head`, seeds demo data, then serves.

```bash
docker compose up --build
```

Then open http://localhost:8000/ . Secrets are read from your shell / a repo-root
`.env` (all optional — the stack boots with safe defaults):

```bash
GEMINI_API_KEY=...        # live AI summaries (else deterministic fallback)
EMAIL_BACKEND=smtp        # real email (else console)
SECRET_KEY=...            # change in production
```

Watch the worker/beat logs to see tasks flowing through Redis (hold expiry every
60s, outbox sweep every 120s, reminders, nightly slot generation).

---

## Configuration

All settings are environment-driven (`pydantic-settings`). Copy
[`backend/.env.example`](backend/.env.example) to `backend/.env` and adjust.
Every value has a safe default — **nothing is required to start.**

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./hcv.db` | DB switch. Postgres: `postgresql+psycopg://user:pass@host:5432/hcv` |
| `SECRET_KEY` | dev value | JWT signing + Fernet key derivation — **change in prod** |
| `BASE_URL` | `http://localhost:8000` | Used for OAuth redirects / links |
| `CLINIC_TZ` | `Asia/Kolkata` | Timezone working hours are expressed in (storage is UTC) |
| `REDIS_URL` | `redis://localhost:6379/0` | Cache, locks, idempotency, rate limit, Celery broker |
| `CELERY_TASK_ALWAYS_EAGER` | `true` | `true` = run tasks inline (no worker). `false` for real topology |
| `HOLD_MINUTES` | `10` | How long a slot stays reserved during form-fill |
| `SLOT_WINDOW_DAYS` | `60` | Rolling horizon of pre-generated slots |
| `LLM_PROVIDER` | `gemini` | `gemini` \| `anthropic` |
| `GEMINI_API_KEY` | *(empty)* | [Free key](https://aistudio.google.com/app/apikey). Empty → deterministic fallback |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini model |
| `EMAIL_BACKEND` | `console` | `console` \| `smtp` \| `sendgrid` |
| `EMAIL_MAX_ATTEMPTS` | `5` | Outbox retries before a message is marked `dead` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | *(empty)* | Google Calendar OAuth (unset → logged no-op) |
| `FERNET_KEY` | *(derived)* | Encrypts OAuth refresh tokens at rest; derived from `SECRET_KEY` if unset |
| `RATE_LIMIT_ENABLED` | `true` | Redis token-bucket limiter on login/register/booking |
| `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` | `admin@clinic.test` / `admin12345` | Seed admin |

---

## AI summaries — prompts (verbatim from the spec)

The prompts are used **exactly** as specified, and each summary persists its
`provider`, `model`, `prompt`, `raw_output`, `latency_ms`, and `status` in the
`summaries` table for auditability ([`app/services/llm/prompts.py`](backend/app/services/llm/prompts.py)).

**Pre-visit** (structured output: `urgency_level ∈ {Low, Medium, High}`,
`chief_complaint`, three `suggested_questions`):

```
Analyse these symptoms and return: urgency level (Low / Medium / High), chief complaint, and three suggested questions for the doctor. Symptoms: {symptoms}
```

**Post-visit** (patient-friendly summary + medication schedule + follow-up):

```
Convert these clinical notes into a patient-friendly summary with medication schedule and follow-up steps: {notes}
```

**Graceful failure:** every provider call is wrapped with a timeout + try/except.
On a missing key, API error, timeout, or schema-validation failure, the pre-visit
path falls back to a deterministic keyword urgency heuristic and the post-visit
path to a templated summary, stored with `status='fallback'`. The booking and
visit flows never break.

---

## Google Calendar setup (optional)

1. In [Google Cloud Console](https://console.cloud.google.com/) create OAuth 2.0
   credentials (Web application).
2. Add the redirect URI: `http://localhost:8000/api/integrations/google/callback`
   (must match `GOOGLE_REDIRECT_URI`).
3. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`.
4. In the app, connect via `GET /api/integrations/google/authorize`. Refresh
   tokens are **Fernet-encrypted at rest**. Calendar events are created on
   confirm, updated on reschedule, and deleted on cancel. Unconfigured → logged
   no-op (no errors).

---

## API overview

Interactive docs at `/docs`. All `/api/*` endpoints accept `Authorization:
Bearer <jwt>`; browser pages use an httpOnly cookie + CSRF token. Bearer requests
are CSRF-exempt.

**Auth** (`/api/auth`)
- `POST /register` · `POST /login` · `POST /logout` · `GET /me`

**Doctors** (`/api/doctors`, any authenticated user)
- `GET /` (search by `?specialisation=`) · `GET /{id}` · `GET /{id}/slots?date=` (cached)

**Appointments** (`/api/appointments`, patient)
- `POST /hold` · `POST /{id}/symptoms` · `POST /{id}/confirm` ·
  `POST /{id}/cancel` · `POST /{id}/reschedule` · `GET /mine`
- `hold`/`confirm` accept an `Idempotency-Key` header (replayed on retry)

**Visits** (`/api/visits`, doctor)
- `GET /schedule` · `GET /appointments/{id}/previsit` · `POST /appointments/{id}/complete`

**Admin** (`/api/admin`, admin only — router-level RBAC)
- `POST /doctors` · `GET /doctors` · `GET /doctors/{id}` · `PATCH /doctors/{id}`
- `PUT /doctors/{id}/working-hours` · `POST /doctors/{id}/leave` ·
  `DELETE /doctors/{id}/leave/{leave_date}`
- `GET /outbox?status=&limit=` (dead-letter / delivery view)

**Integrations** (`/api/integrations/google`)
- `GET /status` · `GET /authorize` · `GET /callback` · `POST /disconnect`

**Health** — `GET /healthz` (liveness) · `GET /readyz` (readiness: DB required,
Redis degraded-only; 503 only if the DB is unreachable)

---

## Background jobs (Celery Beat)

| Job | Task | Cadence |
|---|---|---|
| Expire abandoned holds → return slot to `free` | `holds.expire` | 60s |
| Re-drive pending/failed email outbox | `email.sweep` | 120s |
| Dispatch due medication reminders | `reminders.medications` | 60s |
| Dispatch upcoming-appointment reminders (24h window) | `reminders.appointments` | 300s |
| Roll the slot window forward for all active doctors | `slots.generate` | nightly 02:15 UTC |

Run them in production with:

```bash
celery -A app.celery_app:celery_app worker --loglevel=info
celery -A app.celery_app:celery_app beat --loglevel=info
```

---

## Database schema (11 tables)

`users`, `doctor_profiles`, `doctor_working_hours`, `doctor_leaves`,
**`slots`** (inventory/concurrency — the CAS unit; unique `(doctor_id,
start_time)`), **`appointments`** (clinical record; unique `slot_id`),
`summaries` (stored LLM outputs), `prescriptions`, `medication_reminders`
(unique `(prescription_id, scheduled_at)`), `email_outbox` (unique `dedupe_key`),
`google_credentials` (Fernet-encrypted refresh token). See
[ARCHITECTURE.md](ARCHITECTURE.md) §3 for the ER diagram and the inventory-vs-record
rationale.

**Migrations:** Alembic drives both engines from `DATABASE_URL`
(`render_as_batch` on SQLite). On SQLite the app also auto-creates tables at
startup, so `alembic upgrade head` is optional for local dev but required for
Postgres.

---

## Testing

```bash
cd backend
pytest
```

The suite runs entirely on SQLite (temp file, zero infra) and covers the graded
scenarios: sequential + **threaded** double-hold races (exactly one 201, one
409), idempotency-key replay, availability excluding non-free slots, doctor-leave
cascade + cancellation email, LLM pre-visit fallback (valid `Low/Medium/High`
with no key), RBAC (patient → 403 on admin routes), and the email-outbox
dead-letter path.

---

## Project structure

```
HCV/
├── backend/
│   ├── app/
│   │   ├── main.py            # app factory, logging, request IDs, error handler, routers
│   │   ├── config.py          # pydantic-settings (single DATABASE_URL switch)
│   │   ├── database.py        # engine/SessionLocal/Base + portable UTCDateTime
│   │   ├── models.py          # SQLAlchemy 2.0 models
│   │   ├── security.py        # bcrypt, JWT, cookie/bearer, RBAC, CSRF
│   │   ├── celery_app.py      # Celery instance + Beat schedule
│   │   ├── seed.py            # idempotent demo seeding
│   │   ├── infra/             # redis, cache, locks, idempotency, ratelimit (all degrade)
│   │   ├── routers/           # auth, doctors, appointments, visits, admin, integrations, health, pages
│   │   ├── services/          # booking, llm/, email, calendar, reminders, accounts
│   │   ├── tasks/             # celery: email, calendar, reminders, holds
│   │   ├── templates/         # Jinja2 (login, patient, doctor, admin, book)
│   │   └── static/            # css / htmx
│   ├── alembic/ + alembic.ini # migrations
│   ├── tests/                 # pytest (concurrency, idempotency, leave, llm, rbac, outbox, availability)
│   ├── requirements.txt
│   ├── .env.example
│   └── Dockerfile
├── docker-compose.yml         # web + worker + beat + redis + postgres
├── README.md · DESIGN.md · ARCHITECTURE.md
```

---

## 🚀 Why This Project Stands Out (For Recruiters)

I'm **Parth Jain** — CS undergrad @ VIT Bhopal. I built this project to demonstrate that I don't just write "happy path" code; I build systems that behave correctly under race conditions, partial failures, and real-world edge cases. 

This isn't a simple CRUD demo. Every layer is intentional. My goal was to prove I can **design before I code** (translating a 186-line plan into a 272-line `ARCHITECTURE.md`), **prove correctness logically**, and **ship a resilient system** that runs degraded on `sqlite:///` locally, yet scales effortlessly to `Postgres+Redis+Celery` in production.

### 🧠 Core CS Skills & Architecture Demonstrated
- **Distributed Concurrency & ACID:** Implemented a lock-free Compare-And-Swap (CAS) state machine in `services/booking.py`. This ensures zero double-bookings without relying on DB-specific `SELECT FOR UPDATE` locks.
- **System Resilience (Graceful Degradation):** Designed the architecture so that the core booking flow never breaks. If Redis goes down, caching and rate-limiting fail open. If the LLM times out, a deterministic fallback heuristic kicks in. If the email API fails, an at-least-once Celery outbox pattern queues the message for retry.
- **Advanced State Management:** Separated the domain model into `slots` (inventory/concurrency) and `appointments` (clinical record), ensuring that racing happens only on slots, keeping clinical writes contention-free.
- **Security & Authorization:** Engineered dual-mode authentication (Cookie for UI, Bearer JWT for API). Implemented robust CSRF double-submit tokens and Fernet encryption at-rest for Google OAuth refresh tokens.

### 💡 Key Problem Solving & Decisions
| The Challenge | My Approach | The "Why" |
|---|---|---|
| **Preventing Double Bookings** | Single atomic `UPDATE ... WHERE status` CAS. | It is DB-agnostic (works on both SQLite and Postgres), avoids long transactions, and `rowcount` acts as an absolute proof of victory. |
| **Slot Availability Calculation** | Materialized pre-generated rows instead of dynamic gap calculation. | Gives a natural row-level lock target, enables `ON CONFLICT DO NOTHING` idempotency, and maintains a clean audit trail. |
| **Abandonment during Form-Fill** | A 2-phase hold (`free → held` for 10m). | Safely reserves the slot while patients read the AI summary, with a background Celery Beat sweep to reclaim abandoned holds. |
| **Notification Failures** | Persist-then-send outbox table + 120s Beat sweep. | Guarantees durable, exactly-once delivery (via `dedupe_key`), surviving transient SMTP/Provider outages. |
| **Doctor Leave Conflicts** | Atomic cascade: cancels appointments, enqueues emails, deletes calendar events. | Ensures no data anomalies exist when a doctor takes sudden leave. Bumped cache versions provide O(1) cache invalidation. |

---

## 🔍 How to Test Like an Engineer (Live App Walkthrough)

I want you to try breaking the app at **[https://hcv-parth.up.railway.app](https://hcv-parth.up.railway.app)**. Here is exactly what to notice:

### 1. Upfront Polish & Performance (30s)
* **The Design:** Check out the warm mid-greige, terracotta, and forest UI (`style.css`). It's intentionally designed not to look like another generic "dark mode AI" wrapper.
* **Frictionless Login:** Go to `/login` and tap the demo buttons to instantly fill credentials (`patient@clinic.test`, `dr.rao@clinic.test`, `admin@clinic.test`).
* **Performance:** Notice the speed. `GET /api/doctors` is cached and versioned. Assets are properly cache-busted via mtime.

### 2. The Patient Flow (Concurrency in Action)
* **Start a Hold:** Log in as Patient. Search doctors and pick a slot. Notice the `Idempotency-Key` being sent under the hood (preventing double-creation on network retries).
* **The Countdown:** Watch the live `Hold: MM:SS` countdown timer. It turns red under 1 minute.
* **AI Degradation Test:** Submit symptoms. The app uses a Gemini LLM, but if you look at the backend code, you'll see a deterministic offline fallback heuristic (`_HIGH`/`_MEDIUM`) ready to take over if the AI fails.
* **The Guarantee:** Open a second incognito tab. Try to book the exact same slot. You will immediately hit a `409` conflict — this is the CAS barrier protecting the DB.
* **Rescheduling:** Try to reschedule. The app fetches available slots and only frees your old slot *if* the new CAS hold succeeds.

### 3. Subtle Engineering Details
* **Idempotent Holds:** If you click "Hold" twice on the same slot as the same patient without an idempotency key, it doesn't throw a 409. It returns your *existing* holding appointment. This handles edge cases where Redis is down and rate-limits fail open!
* **UX Nuances:** The Patient appointments page is deliberately collapsed to avoid clutter. Prescriptions intelligently show "pending (+2 more)" previews.
* **Security Checks:** Inspect network requests. Every mutating `POST` echoes an `X-CSRF-Token` header. Rate limiting (`30/300s`) protects the endpoints.

### 4. Doctor & Admin Workflows
* **Doctor Experience:** Log in as a Doctor. Notice how the schedule is auto-sorted, and the AI pre-visit urgency badges help prioritize. Complete a visit with shorthand notes (e.g., `1-0-1`, `PRN`), and see it automatically generate a patient-friendly summary and medication reminders (capped at 30 days via a unique constraint).
* **Admin Experience:** Log in as Admin. Put a doctor on leave. Notice the system's atomicity: it adds `blocked` slots, queues per-patient cancellation emails, and handles calendar deletes. Removing the leave flips the slots back to `free` without erroneously restoring cancelled appointments.

**Deployment Proof:** The app runs with full CI/CD on Railway, mapping `fly.toml` to a `Postgres + Redis + Celery + FastAPI` topology. It boots with `alembic upgrade head` and idempotent data seeding. Legal pages exist (`/privacy#tos`) for OAuth domain consent screens.

## Deployment notes

- **Web:** run `alembic upgrade head` on release, then
  `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers N --proxy-headers`
  behind a TLS-terminating proxy. (Note: uvicorn 0.52 removed `uvicorn.workers`,
  so run uvicorn directly rather than gunicorn + `UvicornWorker`.)
- **Worker + Beat:** one or more `celery worker` processes and exactly one
  `celery beat`.
- **Managed services:** point `DATABASE_URL` at managed Postgres and `REDIS_URL`
  at managed Redis. Set `CELERY_TASK_ALWAYS_EAGER=false`, a strong `SECRET_KEY`,
  `COOKIE_SECURE=true`, and a real `EMAIL_BACKEND`. `docker-compose.yml` is a
  ready reference for the service topology (works on Render/Railway/Fly with
  each service mapped to the matching start command).
- **Secrets** are never committed — everything comes from the environment.
