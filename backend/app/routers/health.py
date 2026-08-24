"""Liveness & readiness probes.

* ``/healthz`` — process is up (no dependencies touched).
* ``/readyz``  — DB reachable (required) + Redis reachable (optional/degraded).
  Returns 200 when the DB is up even if Redis is down, because the app is
  designed to run degraded without Redis; 503 only when the DB is unreachable.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..database import SessionLocal
from ..infra.redis import redis_healthy

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz():
    return {"status": "ok"}


@router.get("/readyz")
def readyz():
    db_ok = False
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            db_ok = True
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        db_ok = False

    redis_ok = redis_healthy()
    body = {
        "status": "ok" if db_ok else "unavailable",
        "database": "ok" if db_ok else "down",
        "redis": "ok" if redis_ok else "degraded",
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)
