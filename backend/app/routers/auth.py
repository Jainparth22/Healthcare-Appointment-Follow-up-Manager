"""Auth API (JSON). The HTML login/register forms live in routers/pages.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import BadRequest
from ..infra import ratelimit
from ..models import ROLE_PATIENT, User
from ..schemas import LoginIn, RegisterIn, TokenOut, UserOut
from ..security import (
    clear_auth_cookies,
    create_access_token,
    get_current_user,
    set_auth_cookies,
)
from ..services.accounts import authenticate, register_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/register", response_model=TokenOut, status_code=201)
def register(payload: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)):
    if not ratelimit.allow("register", _client_ip(request), limit=10, window_seconds=3600):
        raise BadRequest("Too many registration attempts; try again later")
    # Self-registration is always a patient account (staff are created by admin).
    user = register_user(
        db,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        phone=payload.phone,
        role=ROLE_PATIENT,
    )
    set_auth_cookies(response, user)
    return TokenOut(access_token=create_access_token(user), role=user.role)


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    if not ratelimit.allow("login", _client_ip(request), limit=10, window_seconds=300):
        raise BadRequest("Too many login attempts; try again later")
    user = authenticate(db, payload.email, payload.password)
    set_auth_cookies(response, user)
    return TokenOut(access_token=create_access_token(user), role=user.role)


@router.post("/logout")
def logout(response: Response):
    clear_auth_cookies(response)
    return {"detail": "logged out"}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
