"""Database engine, session factory, Base and a portable UTC datetime type.

One `DATABASE_URL` switches between SQLite (dev/tests, zero infra) and
PostgreSQL (deploy). We use only portable constructs — no `SELECT FOR UPDATE`,
no PG-only DDL — so both engines behave identically. The booking correctness
guarantee is a lock-free compare-and-swap (see services/booking.py), which
works the same on either engine.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, TypeDecorator, create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings


def utcnow() -> dt.datetime:
    """Timezone-aware 'now' in UTC. Use everywhere instead of naive utcnow()."""
    return dt.datetime.now(dt.timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores tz-aware datetimes as UTC and always returns them tz-aware in UTC.

    SQLite has no native tz storage, so we normalise on the way in and reattach
    UTC on the way out. This makes datetime handling identical across SQLite and
    Postgres.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)


def _make_engine():
    url = settings.DATABASE_URL
    if url.startswith("sqlite"):
        is_memory = ":memory:" in url or url == "sqlite://"
        kwargs = dict(
            connect_args={"check_same_thread": False},
            future=True,
        )
        if is_memory:
            # Share one connection across threads for in-memory tests.
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        return engine

    # PostgreSQL (or any other server DB)
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20, future=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency yielding a session that is always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
