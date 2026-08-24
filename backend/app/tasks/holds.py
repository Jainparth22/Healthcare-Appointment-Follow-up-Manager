"""Hold-lifecycle + slot-generation tasks (periodic).

* ``holds.expire`` — returns abandoned held slots to ``free`` (CAS) and marks
  their appointments ``expired``.
* ``slots.generate`` — nightly rolling-window materialisation for all active
  doctors (idempotent via the slot unique constraint).
"""
from __future__ import annotations

from ..celery_app import celery_app
from ..database import SessionLocal
from ..services.booking import expire_stale_holds, generate_rolling_window


@celery_app.task(name="holds.expire")
def expire_holds_task() -> int:
    db = SessionLocal()
    try:
        return len(expire_stale_holds(db))
    finally:
        db.close()


@celery_app.task(name="slots.generate")
def generate_slots_task(days: int | None = None) -> int:
    db = SessionLocal()
    try:
        return generate_rolling_window(db, days)
    finally:
        db.close()
