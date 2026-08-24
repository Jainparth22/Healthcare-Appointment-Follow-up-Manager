"""Celery application + Beat schedule.

Async work (email delivery, calendar sync) and periodic jobs (hold expiry,
outbox sweep, reminder dispatch, slot generation) run here. With
``CELERY_TASK_ALWAYS_EAGER=true`` (the default) ``.delay()`` runs inline in the
web process — zero broker needed for local dev/tests. In production run a
``celery worker`` + ``celery beat`` against Redis.

Task modules are referenced by ``include`` (import strings) rather than
imported here, so there is no import cycle: tasks import ``celery_app`` from
this module, not the other way round.
"""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from .config import settings

celery_app = Celery(
    "hcv",
    broker=settings.celery_broker,
    backend=settings.celery_backend,
    include=[
        "app.tasks.email",
        "app.tasks.calendar",
        "app.tasks.reminders",
        "app.tasks.holds",
    ],
)

celery_app.conf.update(
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,
    # In eager mode, don't let a task exception bubble into the caller's request.
    task_eager_propagates=False,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_max_tasks_per_child=200,
    worker_prefetch_multiplier=1,
    timezone="UTC",
    enable_utc=True,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    # Keep trying to reach the broker but never hang a web request forever.
    broker_transport_options={"visibility_timeout": 3600},
)

celery_app.conf.beat_schedule = {
    "expire-stale-holds": {"task": "holds.expire", "schedule": 60.0},
    "sweep-email-outbox": {"task": "email.sweep", "schedule": 120.0},
    "dispatch-medication-reminders": {"task": "reminders.medications", "schedule": 60.0},
    "dispatch-appointment-reminders": {"task": "reminders.appointments", "schedule": 300.0},
    # Nightly rolling-window slot generation (clinic runs in UTC internally).
    "generate-slot-window": {"task": "slots.generate", "schedule": crontab(hour=2, minute=15)},
}
