"""Application settings.

Everything is env-driven (pydantic-settings). Safe defaults let the whole
app run with ZERO external infrastructure: SQLite, Celery eager mode, no-op
cache, LLM fallback and console email. Set real values in `.env` for prod.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---- Application ----
    APP_NAME: str = "Healthcare Appointment & Follow-up Manager"
    ENV: str = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = "dev-secret-change-me-in-production"
    BASE_URL: str = "http://localhost:8000"
    # IANA timezone the clinic's working hours are expressed in.
    CLINIC_TZ: str = "Asia/Kolkata"

    # ---- Database (single switch: SQLite dev/tests, Postgres deploy) ----
    DATABASE_URL: str = "sqlite:///./hcv.db"

    # ---- Auth / JWT ----
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 1 day
    COOKIE_NAME: str = "hcv_access"
    CSRF_COOKIE_NAME: str = "hcv_csrf"
    COOKIE_SECURE: bool = False  # True behind HTTPS in prod

    # ---- Redis ----
    REDIS_URL: str = "redis://localhost:6379/0"

    # ---- Celery ----
    # Empty broker/backend fall back to REDIS_URL (see properties below).
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""
    # Eager mode runs tasks inline in-process (no broker needed) — great for
    # local dev and tests. Set false + run a worker for the real topology.
    CELERY_TASK_ALWAYS_EAGER: bool = True

    # ---- Booking / slots ----
    HOLD_MINUTES: int = 10          # how long a slot stays reserved during form-fill
    SLOT_WINDOW_DAYS: int = 60      # rolling horizon of pre-generated slots
    LOCK_TTL_MS: int = 5000         # Redis slot-lock TTL

    # ---- LLM ----
    LLM_PROVIDER: str = "gemini"    # gemini | anthropic
    LLM_TIMEOUT_SECONDS: float = 20.0
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.6-flash"
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-opus-5"

    # ---- Email ----
    EMAIL_BACKEND: str = "console"  # console | smtp | sendgrid
    EMAIL_FROM: str = "clinic@example.com"
    EMAIL_FROM_NAME: str = "City Clinic"
    EMAIL_MAX_ATTEMPTS: int = 5
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SENDGRID_API_KEY: str = ""

    # ---- Google Calendar ----
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/integrations/google/callback"
    GOOGLE_CALENDAR_ID: str = "primary"
    # Fernet key for encrypting OAuth refresh tokens at rest. Derived from
    # SECRET_KEY when unset so the app runs out-of-the-box; set explicitly in prod.
    FERNET_KEY: str = ""

    # ---- Rate limiting ----
    RATE_LIMIT_ENABLED: bool = True

    # ---- Seed / demo ----
    SEED_ADMIN_EMAIL: str = "admin@clinic.test"
    SEED_ADMIN_PASSWORD: str = "admin12345"

    # ---------- Derived helpers ----------
    @property
    def celery_broker(self) -> str:
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def celery_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or self.REDIS_URL

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def fernet_key(self) -> bytes:
        """A valid 32-byte urlsafe-base64 Fernet key.

        Uses FERNET_KEY when provided, else deterministically derives one from
        SECRET_KEY so encryption works with zero configuration.
        """
        if self.FERNET_KEY:
            return self.FERNET_KEY.encode()
        digest = hashlib.sha256(self.SECRET_KEY.encode()).digest()
        return base64.urlsafe_b64encode(digest)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
