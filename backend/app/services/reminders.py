"""Medication-reminder generation from a prescription's frequency text.

Frequency is free text a doctor typed ("twice daily", "every 8 hours",
"1-0-1", "PRN"). We normalise it to a set of clinic-local dose times, then
materialise ``medication_reminders`` rows over a bounded horizon. Generation is
idempotent via the unique ``(prescription_id, scheduled_at)`` constraint, so
re-running never duplicates. Unparseable frequency falls back to once-daily and
is flagged (never crashes the visit-completion flow).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import utcnow
from ..models import REMINDER_PENDING, MedicationReminder, Prescription

logger = logging.getLogger(__name__)
UTC = dt.timezone.utc

# Bound how far ahead we pre-create reminder rows (a Beat job can extend later).
REMINDER_HORIZON_DAYS = 30

# Standard clinic dose times per doses-per-day.
_DOSE_TIMES: dict[int, list[dt.time]] = {
    1: [dt.time(9, 0)],
    2: [dt.time(9, 0), dt.time(21, 0)],
    3: [dt.time(8, 0), dt.time(14, 0), dt.time(20, 0)],
    4: [dt.time(8, 0), dt.time(12, 0), dt.time(16, 0), dt.time(20, 0)],
}
# Morning / afternoon / night for Indian "M-A-N" notation (e.g. 1-0-1).
_MAN_TIMES = [dt.time(8, 0), dt.time(14, 0), dt.time(20, 0)]


def _interval_times(hours: int) -> list[dt.time]:
    """Waking-hours dose times for an 'every N hours' schedule."""
    out: list[dt.time] = []
    h = 8
    while h <= 22:
        out.append(dt.time(h % 24, 0))
        h += hours
    return out or [dt.time(9, 0)]


def parse_frequency(text: str | None) -> tuple[list[dt.time], bool]:
    """Return (dose_times, parsed_ok).

    ``dose_times`` is empty for PRN / as-needed (no scheduled reminders).
    ``parsed_ok`` is False when we couldn't interpret the text and fell back.
    """
    if not text or not text.strip():
        return _DOSE_TIMES[1], True
    t = text.strip().lower()

    if "prn" in t or "as need" in t or "as required" in t or "when required" in t:
        return [], True

    # Indian morning-afternoon-night notation: 1-0-1, 1-1-1, 0-0-1, 1/0/1 ...
    m = re.fullmatch(r"\s*(\d)\s*[-/]\s*(\d)\s*[-/]\s*(\d)\s*", t)
    if m:
        parts = [int(x) for x in m.groups()]
        times = [_MAN_TIMES[i] for i, p in enumerate(parts) if p > 0]
        return (times or _DOSE_TIMES[1]), True

    # "every N hours" / "q8h" / "q 8 h"
    m = re.search(r"every\s+(\d+)\s*(?:hours?|hrs?|h)\b", t) or re.search(r"\bq\s*(\d+)\s*h\b", t)
    if m:
        n = max(1, min(24, int(m.group(1))))
        return _interval_times(n), True

    if any(k in t for k in ("four times", "4 times", "4x", "qid")):
        return _DOSE_TIMES[4], True
    if any(k in t for k in ("three times", "thrice", "3 times", "3x", "tid", "tds")):
        return _DOSE_TIMES[3], True
    if any(k in t for k in ("twice", "two times", "2 times", "2x", "bid", "bd")):
        return _DOSE_TIMES[2], True
    if any(k in t for k in ("once", "one time", "1 time", "1x", "daily", "od", "qd", "every day")):
        return _DOSE_TIMES[1], True

    # Unparseable — safe default, flagged.
    logger.info("unparseable medication frequency %r; defaulting to once daily", text)
    return _DOSE_TIMES[1], False


def times_per_day(text: str | None) -> int:
    times, _ = parse_frequency(text)
    return len(times)


def generate_reminders(db: Session, prescription: Prescription) -> int:
    """Materialise reminder rows for a prescription. Returns count created."""
    times, _ok = parse_frequency(prescription.frequency)
    if not times:  # PRN — nothing scheduled
        return 0

    tz = ZoneInfo(settings.CLINIC_TZ)
    now = utcnow()
    horizon = min(prescription.duration_days, REMINDER_HORIZON_DAYS)
    if prescription.duration_days > REMINDER_HORIZON_DAYS:
        logger.info(
            "prescription %s spans %s days; capping reminder generation at %s",
            prescription.id, prescription.duration_days, REMINDER_HORIZON_DAYS,
        )

    candidates: list[dt.datetime] = []
    for day in range(horizon):
        d = prescription.start_date + dt.timedelta(days=day)
        for tm in times:
            when = dt.datetime.combine(d, tm, tzinfo=tz).astimezone(UTC)
            if when > now:
                candidates.append(when)

    if not candidates:
        return 0

    existing = set(
        db.scalars(
            select(MedicationReminder.scheduled_at).where(
                MedicationReminder.prescription_id == prescription.id
            )
        ).all()
    )
    created = 0
    for when in candidates:
        if when in existing:
            continue
        db.add(
            MedicationReminder(
                prescription_id=prescription.id,
                patient_id=prescription.patient_id,
                scheduled_at=when,
                status=REMINDER_PENDING,
            )
        )
        created += 1

    try:
        db.commit()
    except IntegrityError:  # concurrent generation raced us — safe to ignore
        db.rollback()
    return created
