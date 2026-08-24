"""Account creation & authentication (shared by API and HTML flows)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import Conflict, Unauthorized, UnprocessableEntity
from ..models import ROLE_PATIENT, User
from ..security import hash_password, verify_password
from ..validators import normalise_email, validate_email_address


def register_user(
    db: Session,
    *,
    email: str,
    password: str,
    full_name: str,
    phone: str | None = None,
    role: str = ROLE_PATIENT,
) -> User:
    # Validated HERE, not just in the Pydantic schemas: the HTML /register form
    # posts plain `Form(...)` strings and the seed script calls this directly,
    # so schema-only validation let invalid addresses into the database.
    try:
        email = validate_email_address(email)
    except ValueError as exc:
        raise UnprocessableEntity(str(exc)) from exc
    if not full_name or not full_name.strip():
        raise UnprocessableEntity("Full name is required")
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise Conflict("An account with this email already exists")
    user = User(
        email=email,
        password_hash=hash_password(password),
        full_name=full_name.strip(),
        phone=phone,
        role=role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # race on unique email
        db.rollback()
        raise Conflict("An account with this email already exists")
    db.refresh(user)
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    # Same normalisation as registration, so `Test@Example.com` finds the row
    # stored as `test@example.com`. A malformed address is normalised loosely
    # and simply misses, giving the same 401 as a wrong password.
    user = db.scalar(select(User).where(User.email == normalise_email(email)))
    if user is None or not verify_password(password, user.password_hash):
        raise Unauthorized("Invalid email or password")
    return user
