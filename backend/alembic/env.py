"""Alembic migration environment.

The database URL and target metadata come from the application itself, so a
single ``DATABASE_URL`` drives both the app and migrations (SQLite locally,
Postgres in deployment). ``render_as_batch`` is enabled on SQLite so ALTERs
work there too.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import the app's settings + metadata (all models register on Base.metadata).
from app.config import settings
from app.database import Base
from app import models  # noqa: F401 - ensure every model is imported/registered

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _is_sqlite() -> bool:
    return settings.DATABASE_URL.startswith("sqlite")


def run_migrations_offline() -> None:
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite(),
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
