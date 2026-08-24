"""Shared field validation.

Email validation lives here because the app has three separate entry points for
the same field and they used to disagree with each other:

* **JSON API** (Pydantic schemas) used ``EmailStr``, which rejects RFC 2606
  special-use domains (``.test``, ``.local``, ``localhost``). Every demo
  account in the README and ``app/seed.py`` uses ``@clinic.test``, so
  ``POST /api/auth/login`` answered **422** for the documented credentials and
  ``GET /api/auth/me`` answered **500** — the ``UserOut`` *response* model
  refused to serialise an address that was already in the database.
* **HTML form routes** took a plain ``str = Form(...)`` and did no validation
  at all, happily storing ``totally-not-an-email``.
* **Service layer / seed** (``register_user``) also did no validation.

``validate_email_address`` is the single source of truth: it enforces real
address syntax, normalises the domain to lower case, and *permits* reserved
test domains (RFC 2606 reserves ``.test`` for exactly this purpose) so the
project's own demo accounts work through every path. Deliverability is never
checked — that needs DNS and would make validation non-deterministic offline.
"""
from __future__ import annotations

from typing import Annotated

from email_validator import EmailNotValidError, validate_email
from pydantic import AfterValidator

MAX_EMAIL_LENGTH = 254  # RFC 5321 practical limit


def validate_email_address(value: str) -> str:
    """Return the normalised address, or raise ``ValueError`` with a clear message."""
    if not isinstance(value, str):
        raise ValueError("Email address is required")
    candidate = value.strip()
    if not candidate:
        raise ValueError("Email address is required")
    if len(candidate) > MAX_EMAIL_LENGTH:
        raise ValueError("Email address is too long")
    try:
        info = validate_email(
            candidate,
            check_deliverability=False,  # no DNS: keeps validation offline/deterministic
            test_environment=True,       # allow RFC 2606 .test/.local demo domains
        )
    except EmailNotValidError as exc:
        # `str(exc)` is already user-facing ("The email address is not valid.
        # It must have exactly one @-sign."), so don't prefix it.
        raise ValueError(str(exc)) from exc
    # `normalized` lower-cases the domain; lower the local part too so lookups
    # and the UNIQUE constraint on users.email behave case-insensitively.
    return info.normalized.lower()


def normalise_email(value: str) -> str:
    """Best-effort normalisation for *lookups* (login).

    Login must not tell an attacker whether an address was merely malformed —
    that is a different answer from "wrong password". So an invalid address is
    normalised loosely here and simply fails to match any row, yielding the
    same 401 as a bad password.
    """
    try:
        return validate_email_address(value)
    except ValueError:
        return (value or "").strip().lower()


#: Drop-in replacement for ``pydantic.EmailStr`` used by every schema.
Email = Annotated[str, AfterValidator(validate_email_address)]
