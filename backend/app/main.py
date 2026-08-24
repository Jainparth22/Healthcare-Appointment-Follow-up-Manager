"""FastAPI application factory.

Ties the whole service together: structured logging, per-request correlation
IDs, a single ``DomainError -> JSON`` exception handler, static assets, the
Jinja HTML pages, and every JSON router. On SQLite it creates tables on
startup so the app runs with zero infra (``alembic upgrade head`` is used for
Postgres / migrations).
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401 - ensure models register on Base.metadata
from .config import settings
from .database import Base, engine
from .errors import DomainError
from .logging_config import configure_logging, log, request_id_var
from .routers import (
    admin,
    appointments,
    auth,
    doctors,
    health,
    integrations,
    pages,
    visits,
)

logger = logging.getLogger("app.access")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.LOG_LEVEL)
    # SQLite (dev/tests) has no migration story — create tables directly.
    # Postgres deployments run `alembic upgrade head` instead.
    if settings.is_sqlite:
        Base.metadata.create_all(bind=engine)
    log(logger, logging.INFO, "startup", env=settings.ENV, sqlite=settings.is_sqlite)
    yield
    log(logger, logging.INFO, "shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---- Request-ID + access logging middleware ------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - log then re-raise to the 500 handler
            elapsed = (time.perf_counter() - started) * 1000
            log(
                logger, logging.ERROR, "request_failed",
                method=request.method, path=request.url.path, duration_ms=round(elapsed, 1),
            )
            request_id_var.reset(token)
            raise
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = rid
        log(
            logger, logging.INFO, "request",
            method=request.method, path=request.url.path,
            status=response.status_code, duration_ms=round(elapsed, 1),
        )
        request_id_var.reset(token)
        return response

    # ---- Domain errors -> JSON ----------------------------------------
    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    # ---- Routers -------------------------------------------------------
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(doctors.router)
    app.include_router(appointments.router)
    app.include_router(visits.router)
    app.include_router(admin.router)
    app.include_router(integrations.router)
    app.include_router(pages.router)  # HTML pages last (owns "/")

    return app


app = create_app()
