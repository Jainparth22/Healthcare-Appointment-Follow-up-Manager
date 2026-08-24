"""Google Calendar integration.

* OAuth 2.0 connect/callback per user.
* Refresh tokens are encrypted at rest with Fernet.
* Create/update/delete events for a user's primary calendar.
* Fully optional: when Google isn't configured, or a user hasn't connected,
  every operation is a logged no-op (never a 500). Token refresh failures mark
  the credential disconnected instead of raising.
"""
from __future__ import annotations

import datetime as dt
import logging

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import utcnow
from ..models import GoogleCredential

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


# --------------------------------------------------------------------------
# Crypto
# --------------------------------------------------------------------------
def _fernet() -> Fernet:
    return Fernet(settings.fernet_key)


def _encrypt(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Configuration / OAuth
# --------------------------------------------------------------------------
def is_configured() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def _client_config() -> dict:
    return {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
        }
    }


def _flow(state: str | None = None):
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, state=state)
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    return flow


def authorize_url(state: str, code_verifier: str | None = None) -> str | None:
    if not is_configured():
        return None
    flow = _flow(state=state)
    if code_verifier:
        flow.code_verifier = code_verifier
    url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    return url


def handle_callback(db: Session, user_id: int, code: str, state: str | None, code_verifier: str | None = None) -> bool:
    if not is_configured():
        return False
    flow = _flow(state=state)
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    _store_credentials(db, user_id, creds)
    return True


def _store_credentials(db: Session, user_id: int, creds) -> None:
    row = db.scalar(select(GoogleCredential).where(GoogleCredential.user_id == user_id))
    if row is None:
        row = GoogleCredential(user_id=user_id)
        db.add(row)
    row.enc_token = _encrypt(creds.token)
    if creds.refresh_token:  # only present on first consent
        row.enc_refresh_token = _encrypt(creds.refresh_token)
    row.token_uri = creds.token_uri
    row.client_id = creds.client_id
    row.scopes = " ".join(creds.scopes or SCOPES)
    if getattr(creds, "expiry", None):
        row.expiry = creds.expiry.replace(tzinfo=dt.timezone.utc) if creds.expiry.tzinfo is None else creds.expiry
    row.connected = True
    row.updated_at = utcnow()
    db.commit()


def get_status(db: Session, user_id: int) -> dict:
    row = db.scalar(select(GoogleCredential).where(GoogleCredential.user_id == user_id))
    return {
        "configured": is_configured(),
        "connected": bool(row and row.connected and row.enc_refresh_token),
    }


def disconnect(db: Session, user_id: int) -> None:
    row = db.scalar(select(GoogleCredential).where(GoogleCredential.user_id == user_id))
    if row:
        row.connected = False
        db.commit()


# --------------------------------------------------------------------------
# Credential loading (with refresh + persistence)
# --------------------------------------------------------------------------
def _load_credentials(db: Session, user_id: int):
    if not is_configured():
        return None
    row = db.scalar(select(GoogleCredential).where(GoogleCredential.user_id == user_id))
    if row is None or not row.connected or not row.enc_refresh_token:
        return None

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token=_decrypt(row.enc_token),
        refresh_token=_decrypt(row.enc_refresh_token),
        token_uri=row.token_uri or "https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=(row.scopes or "").split() or SCOPES,
    )
    try:
        if not creds.valid:
            creds.refresh(Request())
            _store_credentials(db, user_id, creds)
        return creds
    except Exception as exc:  # noqa: BLE001 - token revoked/expired
        logger.warning("Google token refresh failed for user %s (%s); disconnecting", user_id, exc)
        row.connected = False
        db.commit()
        return None


def _service(creds):
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _event_body(summary: str, description: str, start_dt: dt.datetime, end_dt: dt.datetime) -> dict:
    return {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.astimezone(dt.timezone.utc).isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end_dt.astimezone(dt.timezone.utc).isoformat(), "timeZone": "UTC"},
    }


# --------------------------------------------------------------------------
# Per-user event operations (safe no-ops on failure)
# --------------------------------------------------------------------------
def create_event_for_user(
    db: Session, user_id: int, *, summary: str, description: str,
    start_dt: dt.datetime, end_dt: dt.datetime,
) -> str | None:
    creds = _load_credentials(db, user_id)
    if creds is None:
        logger.info("calendar: user %s not connected; skipping create", user_id)
        return None
    try:
        event = (
            _service(creds)
            .events()
            .insert(calendarId=settings.GOOGLE_CALENDAR_ID, body=_event_body(summary, description, start_dt, end_dt))
            .execute()
        )
        return event.get("id")
    except Exception as exc:  # noqa: BLE001
        logger.warning("calendar create failed for user %s (%s)", user_id, exc)
        return None


def update_event_for_user(
    db: Session, user_id: int, event_id: str, *, start_dt: dt.datetime, end_dt: dt.datetime
) -> bool:
    creds = _load_credentials(db, user_id)
    if creds is None or not event_id:
        return False
    try:
        _service(creds).events().patch(
            calendarId=settings.GOOGLE_CALENDAR_ID,
            eventId=event_id,
            body={
                "start": {"dateTime": start_dt.astimezone(dt.timezone.utc).isoformat(), "timeZone": "UTC"},
                "end": {"dateTime": end_dt.astimezone(dt.timezone.utc).isoformat(), "timeZone": "UTC"},
            },
        ).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("calendar update failed for user %s (%s)", user_id, exc)
        return False


def delete_event_for_user(db: Session, user_id: int, event_id: str) -> bool:
    creds = _load_credentials(db, user_id)
    if creds is None or not event_id:
        return False
    try:
        _service(creds).events().delete(
            calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id
        ).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("calendar delete failed for user %s (%s)", user_id, exc)
        return False
