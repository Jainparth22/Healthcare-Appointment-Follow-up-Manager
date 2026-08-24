"""Google Calendar OAuth (JSON + redirects).

``authorize`` builds a consent URL with a signed, short-lived ``state`` token
that encodes the user id (stateless — no server-side session needed).
``callback`` verifies it, exchanges the code, and stores encrypted credentials.
All endpoints degrade to a clear message when Google isn't configured.
"""
from __future__ import annotations

import datetime as dt

import jwt
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

import logging

from ..config import settings
from ..database import get_db
from ..models import ROLE_ADMIN, ROLE_DOCTOR, User
from ..security import get_current_user
from ..services import calendar as gcal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations/google", tags=["integrations"])


def _state_token(user_id: int, code_verifier: str | None = None) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload: dict = {"sub": str(user_id), "purpose": "gcal", "exp": now + dt.timedelta(minutes=10)}
    if code_verifier:
        payload["cv"] = code_verifier
    return jwt.encode(
        payload,
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def _state_user(state: str | None) -> int | None:
    if not state:
        return None
    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("purpose") != "gcal":
        return None
    try:
        return int(payload["sub"])
    except (KeyError, ValueError):
        return None


def _state_verifier(state: str | None) -> str | None:
    if not state:
        return None
    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    return payload.get("cv")


def _home_for(role: str) -> str:
    if role == ROLE_ADMIN:
        return "/admin"
    if role == ROLE_DOCTOR:
        return "/doctor"
    return "/patient"


@router.get("/status")
def google_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return gcal.get_status(db, user.id)


@router.get("/authorize")
def google_authorize(user: User = Depends(get_current_user)):
    if not gcal.is_configured():
        return {"configured": False, "detail": "Google Calendar is not configured on this server"}
    import secrets as _secrets
    verifier = _secrets.token_urlsafe(64)
    state = _state_token(user.id, verifier)
    url = gcal.authorize_url(state, verifier)
    return RedirectResponse(url, status_code=307)


@router.get("/callback")
def google_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    user_id = _state_user(state)
    if error:
        logger.warning("google callback error param %s (request_id logged)", error)
        return RedirectResponse("/?google=error", status_code=303)
    if not code or user_id is None:
        logger.warning("google callback missing code or invalid state (user_id=%s)", user_id)
        return RedirectResponse("/?google=error", status_code=303)
    user = db.get(User, user_id)
    if user is None:
        logger.warning("google callback user not found %s", user_id)
        return RedirectResponse("/?google=error", status_code=303)
    verifier = _state_verifier(state)
    try:
        gcal.handle_callback(db, user_id, code, state, verifier)
    except Exception as exc:  # noqa: BLE001 - surface as a soft error, never a 500 page
        logger.warning("google handle_callback failed for user %s: %s", user_id, exc)
        return RedirectResponse(f"{_home_for(user.role)}?google=error", status_code=303)
    return RedirectResponse(f"{_home_for(user.role)}?google=connected", status_code=303)


@router.post("/disconnect")
def google_disconnect(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    gcal.disconnect(db, user.id)
    return {"detail": "disconnected"}
