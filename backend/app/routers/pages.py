"""Server-rendered HTML pages (Jinja2 + HTMX).

The pages are thin shells: they enforce the role gate and render layout, then
``app.js`` drives the dynamic flows by calling the JSON APIs with the session
cookie (echoing the CSRF token from the ``hcv_csrf`` cookie). Login/register
forms are handled here because they must set the auth cookies on the response.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..errors import DomainError
from ..models import ROLE_ADMIN, ROLE_DOCTOR, ROLE_PATIENT
from ..security import clear_auth_cookies, get_optional_user, set_auth_cookies, verify_csrf
from ..services import calendar as gcal
from ..services.accounts import authenticate, register_user
from ..infra import ratelimit

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def asset_url(path: str) -> str:
    """Static URL with an mtime cache-buster (``/static/app.js?v=<mtime>``).

    The version changes whenever the file changes on disk, so browsers always
    refetch an edited asset (no more stale cached JS after a deploy or an edit)
    while still caching between changes. Falls back to a fixed tag if the file
    can't be stat'd.
    """
    rel = path.lstrip("/")
    if rel.startswith("static/"):
        rel = rel[len("static/"):]
    try:
        ver = int((STATIC_DIR / rel).stat().st_mtime)
    except OSError:
        ver = 0
    return f"/static/{rel}?v={ver}"


# Available in every template as {{ asset_url('app.js') }}.
templates.env.globals["asset_url"] = asset_url

router = APIRouter(tags=["pages"], include_in_schema=False)


def _home_for(role: str) -> str:
    return {ROLE_ADMIN: "/admin", ROLE_DOCTOR: "/doctor"}.get(role, "/patient")


def _ctx(request: Request, **extra) -> dict:
    base = {
        "request": request,
        "app_name": settings.APP_NAME,
        "google_configured": gcal.is_configured(),
    }
    base.update(extra)
    return base


def render(request: Request, name: str, *, status_code: int = 200, **extra):
    # Starlette >=1.x requires `request` as the first positional arg.
    return templates.TemplateResponse(request, name, _ctx(request, **extra), status_code=status_code)


# --------------------------------------------------------------------------
# Auth pages (forms set cookies)
# --------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    user = get_optional_user(request, db)
    if user is not None:
        return RedirectResponse(_home_for(user.role), status_code=303)
    return render(request, "login.html")


@router.post("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    if not ratelimit.allow("login", ip, limit=10, window_seconds=300):
        return render(request, "login.html", status_code=429, error="Too many attempts; try again later.")
    try:
        user = authenticate(db, email, password)
    except DomainError:
        return render(request, "login.html", status_code=401, error="Invalid email or password.")
    resp = RedirectResponse(_home_for(user.role), status_code=303)
    set_auth_cookies(resp, user)
    return resp


@router.post("/register", response_class=HTMLResponse)
def register_form(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    phone: str = Form(default=""),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    if not ratelimit.allow("register", ip, limit=10, window_seconds=3600):
        return render(request, "login.html", status_code=429, error="Too many attempts; try again later.", register_open=True)
    try:
        user = register_user(
            db, email=email, password=password, full_name=full_name,
            phone=phone or None, role=ROLE_PATIENT,
        )
    except DomainError as exc:
        return render(
            request, "login.html", status_code=exc.status_code, error=exc.detail, register_open=True
        )
    resp = RedirectResponse("/patient", status_code=303)
    set_auth_cookies(resp, user)
    return resp


@router.post("/logout")
def logout_form(request: Request, _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    resp = RedirectResponse("/", status_code=303)
    clear_auth_cookies(resp)
    return resp


# --------------------------------------------------------------------------
# Role dashboards (shells; data loaded client-side from the JSON APIs)
# --------------------------------------------------------------------------
def _guard(request: Request, db: Session, role: str):
    user = get_optional_user(request, db)
    if user is None:
        return None, RedirectResponse("/", status_code=303)
    if user.role != role:
        return None, RedirectResponse(_home_for(user.role), status_code=303)
    return user, None


@router.get("/patient", response_class=HTMLResponse)
def patient_home(request: Request, db: Session = Depends(get_db)):
    user, redirect = _guard(request, db, ROLE_PATIENT)
    if redirect:
        return redirect
    return render(request, "patient.html", user=user)


@router.get("/patient/book/{doctor_id}", response_class=HTMLResponse)
def patient_book(doctor_id: int, request: Request, db: Session = Depends(get_db)):
    user, redirect = _guard(request, db, ROLE_PATIENT)
    if redirect:
        return redirect
    return render(request, "book.html", user=user, doctor_id=doctor_id)


@router.get("/doctor", response_class=HTMLResponse)
def doctor_home(request: Request, db: Session = Depends(get_db)):
    user, redirect = _guard(request, db, ROLE_DOCTOR)
    if redirect:
        return redirect
    return render(request, "doctor.html", user=user)


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, db: Session = Depends(get_db)):
    user, redirect = _guard(request, db, ROLE_ADMIN)
    if redirect:
        return redirect
    return render(request, "admin.html", user=user)
