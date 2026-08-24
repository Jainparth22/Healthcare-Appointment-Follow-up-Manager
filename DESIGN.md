# DESIGN.md — Healthcare Appointment & Follow-up Manager

This is the required design write-up covering the four graded hard problems:
double-booking prevention, slot-hold mechanism, doctor-leave conflict handling,
and notification-failure handling. It also states how the system degrades when
external dependencies (LLM, email, calendar, Redis) are unavailable.

## Model: inventory vs. record

Concurrency and clinical data are deliberately split across two tables:

- **`slots`** — the *inventory / concurrency* unit: `(doctor_id, start_time)`,
  `status ∈ {free, held, booked, blocked}`, `held_by`, `hold_expires_at`,
  `version`. A unique constraint on `(doctor_id, start_time)` makes slot
  generation idempotent and is the last-resort integrity net.
- **`appointments`** — the *clinical record*: symptoms, notes, summaries,
  calendar event IDs, `status ∈ {holding, confirmed, completed, cancelled,
  expired}`, referencing one slot.

Racing happens only on `slots`; clinical writes never contend. This is what
keeps every state transition cheap and race-safe.

## Double-booking prevention (layered, lock-free CAS)

A booking is a **compare-and-swap** — one atomic UPDATE whose row-count *is* the
verdict — guarded by three independent layers:

1. **L1 — Redis lock** (`SET lock:slot:{id} <token> NX PX 5000`) taken before the
   CAS. It gives fast, cross-worker rejection and a clean 409 under bursts, and
   is released with a Lua compare-and-delete. It is an *optimization only*: if
   Redis is down the lock **fails open** and correctness still holds.
2. **L2 — the CAS (the guarantee):**
   `UPDATE slots SET status='held', held_by=:p, hold_expires_at=:exp,
   version=version+1 WHERE id=:id AND status='free'`.
   `rowcount == 1` → the caller won; `0` → someone else won → **409**. No
   `SELECT ... FOR UPDATE`, no application locks, and **identical behaviour on
   SQLite and Postgres**. Two simultaneous holders cannot both get `rowcount==1`
   because the row's `status` can satisfy `= 'free'` for exactly one UPDATE.
3. **L3 — unique `(doctor_id, start_time)`** — a structural net that makes a
   duplicate physically impossible even if the layers above were bypassed.

Because the guarantee lives in the database row, not in Redis or Python, the
correctness proof does not depend on any single process, and it does not depend
on Postgres-only features. Confirm, cancel, reschedule and hold-expiry are the
*same* CAS pattern with different `WHERE` guards, so the whole lifecycle is
race-safe by construction.

**Idempotency:** hold/confirm accept an `Idempotency-Key`; the first outcome is
cached in Redis and replayed on retry, so double-clicks and client retries never
create a second appointment. When Redis is down this is a no-op — which is safe,
because the CAS already prevents duplicate state.

## Slot-hold mechanism (two-phase)

Booking is deliberately two-phase so the slot is genuinely reserved while the
patient fills in symptoms and reviews the AI pre-visit summary:

1. `POST /hold` → L1 lock + L2 CAS `free→held` with
   `hold_expires_at = now + HOLD_MINUTES`, then create the `holding` appointment.
2. Patient submits symptoms → pre-visit summary generated and stored.
3. `POST /confirm` → CAS `held→booked` **guarded by `held_by = :p AND
   hold_expires_at > now`**. If the hold expired or was taken, `rowcount == 0`
   → 409 with a "re-pick a slot" prompt; otherwise the appointment becomes
   `confirmed` and email + calendar jobs are enqueued.

A **Celery Beat** sweep (every 60s) returns abandoned holds to inventory:
`... SET status='free', held_by=NULL WHERE status='held' AND hold_expires_at <
now`, moving the appointment to `expired`. Nothing leaks if a patient walks away.

## Doctor-leave conflict handling

`POST /api/admin/doctors/{id}/leave` records the date (unique
`(doctor_id, leave_date)` → marking leave twice is idempotent), then in one
transaction: cancels every `held`/`booked` appointment that day (detaching the
slot), **enqueues a cancellation email per affected patient** and requests
deletion of their calendar events, and flips those slots to `blocked` so nobody
can rebook. The doctor's cache version is bumped for O(1) invalidation. It
returns the affected count. Removing leave flips `blocked → free`.

## Notification-failure handling (at-least-once outbox)

Email is never sent inline. Every message is first persisted to `email_outbox`
with a unique `dedupe_key`, *then* a Celery task delivers it. On failure
`attempts` is incremented and the row is retried with backoff up to
`max_attempts`, after which it is marked `dead` (visible to admins). A Beat sweep
re-drives pending rows every 120s, so a transient SMTP/provider outage self-heals
and the `dedupe_key` guarantees no duplicate is ever sent. This turns "send an
email" from a fragile synchronous call into a durable, retryable, exactly-once-
observable operation.

## Graceful degradation

Every external dependency has a safe fallback so the core booking flow never
breaks: **LLM** → deterministic keyword-based urgency heuristic / templated
summary, stored with `status='fallback'`; **email** → console backend prints to
logs; **calendar** → logged no-op when OAuth is unconfigured; **Redis** → cache
no-ops, locks fail open, rate-limiting fails open, and Celery runs tasks eagerly
in-process. The result is a system that runs with **zero external infrastructure**
locally yet scales to the full Redis + Celery + Postgres topology unchanged.
