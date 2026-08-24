"""Security: password hashing, JWT, cookie/bearer auth, RBAC and CSRF.

Tokens are accepted two ways:
* ``Authorization: Bearer <jwt>`` — for JSON/API clients.
* an httpOnly cookie — for the server-rendered HTML pages.

CSRF: cookie-authenticated form posts must echo a double-submit token
(cookie value must equal the ``X-CSRF-Token`` header or ``csrf_token`` form
field). Bearer requests carry no ambient credential and are exempt.
"""
from __future__ import annotations

import datetime as dt
import hmac
import secrets

import bcrypt
import jwt
from fastapi import Depends, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User

_BCRYPT_MAX = 72  # bcrypt only considers the first 72 bytes


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    pw = password.encode("utf-8")[:_BCRYPT_MAX]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        pw = password.encode("utf-8")[:_BCRYPT_MAX]
        return bcrypt.checkpw(pw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def create_access_token(user: User) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": now + dt.timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


# --------------------------------------------------------------------------
# Current-user resolution
# --------------------------------------------------------------------------
def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(settings.COOKIE_NAME)


def _user_from_token(token: str | None, db: Session) -> User | None:
    if not token:
        return None
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    return db.get(User, int(sub))


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Require a valid identity (bearer or cookie). 401 otherwise."""
    user = _user_from_token(_extract_token(request), db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Return the user if authenticated, else None (for public/HTML pages)."""
    return _user_from_token(_extract_token(request), db)


def require_role(*roles: str):
    """Dependency factory enforcing that the caller has one of ``roles``."""

    def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return _dep


# --------------------------------------------------------------------------
# CSRF (double-submit token)
# --------------------------------------------------------------------------
def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf(
    request: Request,
    csrf_token: str | None = Form(default=None),
) -> None:
    """CSRF guard for cookie-authenticated form posts.

    Skipped when the request carries a Bearer token (no ambient credential to
    abuse). Compares the cookie token against the header or form field.
    """
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return
    cookie_token = request.cookies.get(settings.CSRF_COOKIE_NAME)
    submitted = request.headers.get("X-CSRF-Token") or csrf_token
    if not cookie_token or not submitted or not hmac.compare_digest(cookie_token, submitted):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")


def verify_csrf_header(request: Request) -> None:
    """CSRF guard for JSON/HTMX endpoints (header-only, no Form field).

    Used where the request body is JSON — declaring a ``Form`` field (as
    ``verify_csrf`` does) would force multipart parsing and break the JSON body.
    Bearer requests are exempt (no ambient credential).
    """
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return
    cookie_token = request.cookies.get(settings.CSRF_COOKIE_NAME)
    submitted = request.headers.get("X-CSRF-Token")
    if not cookie_token or not submitted or not hmac.compare_digest(cookie_token, submitted):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")


def set_auth_cookies(response, user: User) -> str:
    """Set the auth + CSRF cookies on a response. Returns the CSRF token."""
    token = create_access_token(user)
    csrf = generate_csrf_token()
    response.set_cookie(
        settings.COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        settings.CSRF_COOKIE_NAME,
        csrf,
        httponly=False,  # JS/template must read it to echo back
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return csrf


def clear_auth_cookies(response) -> None:
    response.delete_cookie(settings.COOKIE_NAME)
    response.delete_cookie(settings.CSRF_COOKIE_NAME)
