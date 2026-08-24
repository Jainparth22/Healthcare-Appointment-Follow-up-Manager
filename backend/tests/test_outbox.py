"""(g) Notification reliability — a message that keeps failing to send is
retried and lands in the dead-letter state after max_attempts, never lost and
never raising into the caller."""
from __future__ import annotations

import app.services.email as email_mod
from app.models import OUTBOX_DEAD, OUTBOX_FAILED, OUTBOX_PENDING, EmailOutbox
from app.services.email import deliver_outbox


def test_outbox_dead_letters_after_max_attempts(db, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(email_mod, "send_message", boom)

    row = EmailOutbox(
        to_email="p@t.test",
        subject="hi",
        body_text="body",
        kind="test",
        status=OUTBOX_PENDING,
        attempts=0,
        max_attempts=2,
        dedupe_key="dead-letter-1",
    )
    db.add(row)
    db.commit()
    outbox_id = row.id

    assert deliver_outbox(outbox_id) == OUTBOX_FAILED  # attempt 1 of 2
    assert deliver_outbox(outbox_id) == OUTBOX_DEAD  # attempt 2 → dead

    db.expire_all()
    reloaded = db.get(EmailOutbox, outbox_id)
    assert reloaded.status == OUTBOX_DEAD
    assert reloaded.attempts == 2
    assert "smtp down" in reloaded.last_error


def test_outbox_stops_attempting_once_dead(db, monkeypatch):
    """A dead row is terminal — further deliver calls are no-ops."""
    monkeypatch.setattr(email_mod, "send_message", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    row = EmailOutbox(
        to_email="p@t.test", subject="s", body_text="b", kind="test",
        status=OUTBOX_PENDING, attempts=0, max_attempts=1, dedupe_key="dead-letter-2",
    )
    db.add(row)
    db.commit()
    outbox_id = row.id

    assert deliver_outbox(outbox_id) == OUTBOX_DEAD
    # Second call returns the terminal status without incrementing attempts.
    assert deliver_outbox(outbox_id) == OUTBOX_DEAD
    db.expire_all()
    assert db.get(EmailOutbox, outbox_id).attempts == 1
