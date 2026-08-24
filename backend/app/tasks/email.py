"""Email delivery tasks.

``send_email_task`` attempts one outbox row. It does NOT use Celery-level
retries — ``deliver_outbox`` records the failure on the row and the periodic
``sweep_outbox_task`` re-attempts anything still ``pending``/``failed`` with
attempts remaining. This keeps behaviour identical in eager mode (no retry
loops) and gives at-least-once delivery that survives broker outages.
"""
from __future__ import annotations

from ..celery_app import celery_app
from ..services.email import deliver_outbox, dispatch_pending


@celery_app.task(name="email.send")
def send_email_task(outbox_id: int) -> str:
    return deliver_outbox(outbox_id)


@celery_app.task(name="email.sweep")
def sweep_outbox_task() -> int:
    return dispatch_pending()
