"""Email service: pluggable backends + a durable outbox.

Delivery is **at-least-once**: every message is persisted to ``email_outbox``
(with a unique ``dedupe_key``) before any send attempt. A Celery task sends it;
failures back off and retry up to ``max_attempts`` then land in ``dead``. A
periodic sweep (``dispatch_pending``) also picks up anything still pending, so
even if the enqueue-time ``.delay()`` never fired (e.g. broker was down) the
mail still goes out. ``dedupe_key`` guarantees no double-send.
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import SessionLocal, utcnow
from ..models import (
    OUTBOX_DEAD,
    OUTBOX_FAILED,
    OUTBOX_PENDING,
    OUTBOX_SENT,
    EmailOutbox,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------
def _send_console(to_email: str, subject: str, text: str, html: str | None) -> None:
    logger.info(
        "EMAIL (console backend)",
        extra={"extra_fields": {"to": to_email, "subject": subject}},
    )
    # Also print so it is obvious in local dev stdout.
    print(f"\n===== EMAIL → {to_email} =====\nSubject: {subject}\n{text}\n============================\n")


def _send_smtp(to_email: str, subject: str, text: str, html: str | None) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.EMAIL_FROM_NAME} <{settings.EMAIL_FROM}>"
    msg["To"] = to_email
    msg.attach(MIMEText(text, "plain"))
    if html:
        msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as server:
        if settings.SMTP_USE_TLS:
            server.starttls()
        if settings.SMTP_USER:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(settings.EMAIL_FROM, [to_email], msg.as_string())


def _send_sendgrid(to_email: str, subject: str, text: str, html: str | None) -> None:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(
        from_email=settings.EMAIL_FROM,
        to_emails=to_email,
        subject=subject,
        plain_text_content=text,
        html_content=html or f"<pre>{text}</pre>",
    )
    client = SendGridAPIClient(settings.SENDGRID_API_KEY)
    resp = client.send(message)
    if resp.status_code >= 400:
        raise RuntimeError(f"SendGrid error {resp.status_code}")


def send_message(to_email: str, subject: str, text: str, html: str | None = None) -> None:
    """Send synchronously via the configured backend. Raises on failure."""
    backend = settings.EMAIL_BACKEND.lower()
    if backend == "smtp":
        _send_smtp(to_email, subject, text, html)
    elif backend == "sendgrid":
        _send_sendgrid(to_email, subject, text, html)
    else:
        _send_console(to_email, subject, text, html)


# --------------------------------------------------------------------------
# Outbox
# --------------------------------------------------------------------------
def enqueue_email(
    *,
    to_email: str,
    subject: str,
    text: str,
    html: str | None = None,
    kind: str,
    dedupe_key: str,
    related_appointment_id: int | None = None,
) -> int | None:
    """Persist an email to the outbox (idempotent on ``dedupe_key``).

    Returns the outbox id, or ``None`` if a message with this key already
    exists. Triggers the async send task; the periodic sweep is the backstop.
    """
    db = SessionLocal()
    try:
        existing = db.scalar(select(EmailOutbox).where(EmailOutbox.dedupe_key == dedupe_key))
        if existing is not None:
            return None
        row = EmailOutbox(
            to_email=to_email,
            subject=subject,
            body_text=text,
            body_html=html,
            kind=kind,
            status=OUTBOX_PENDING,
            max_attempts=settings.EMAIL_MAX_ATTEMPTS,
            related_appointment_id=related_appointment_id,
            dedupe_key=dedupe_key,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
        outbox_id = row.id
    finally:
        db.close()

    # Fire the async send; if the broker/import is unavailable the sweep covers it.
    try:
        from ..tasks.email import send_email_task

        send_email_task.delay(outbox_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not enqueue send task (%s); sweep will handle it", exc)
    return outbox_id


def deliver_outbox(outbox_id: int) -> str:
    """Attempt to send one outbox row. Returns its resulting status.

    Never raises — records the error on the row instead so the caller (task or
    sweep) can decide whether to retry based on the returned status.
    """
    db = SessionLocal()
    try:
        row = db.get(EmailOutbox, outbox_id)
        if row is None or row.status in (OUTBOX_SENT, OUTBOX_DEAD):
            return row.status if row else "missing"
        try:
            send_message(row.to_email, row.subject, row.body_text, row.body_html)
            row.status = OUTBOX_SENT
            row.sent_at = utcnow()
            db.commit()
            return OUTBOX_SENT
        except Exception as exc:  # noqa: BLE001
            row.attempts += 1
            row.last_error = str(exc)[:2000]
            row.status = OUTBOX_DEAD if row.attempts >= row.max_attempts else OUTBOX_FAILED
            db.commit()
            logger.warning(
                "email send failed",
                extra={"extra_fields": {"outbox_id": outbox_id, "attempts": row.attempts, "status": row.status}},
            )
            return row.status
    finally:
        db.close()


def dispatch_pending(limit: int = 100) -> int:
    """Sweep: (re)send pending/failed rows that still have attempts left."""
    db = SessionLocal()
    try:
        now = utcnow()
        rows = db.scalars(
            select(EmailOutbox)
            .where(
                EmailOutbox.status.in_((OUTBOX_PENDING, OUTBOX_FAILED)),
                EmailOutbox.attempts < EmailOutbox.max_attempts,
                EmailOutbox.scheduled_at <= now,
            )
            .limit(limit)
        ).all()
        ids = [r.id for r in rows]
    finally:
        db.close()
    for outbox_id in ids:
        deliver_outbox(outbox_id)
    return len(ids)
