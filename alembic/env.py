"""Alembic environment, driven from :mod:`app.config` rather than alembic.ini.

The database URL is never written in the ini file: tests and the CLI both point
``HOLAFRESCA_DB_PATH`` at a throwaway file, and a migration that ignored that
would rewrite the real library. :mod:`app.db.session` also runs these migrations
in-process on startup, handing its own connection over via ``config.attributes``.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import config as app_config
from app.db import models  # noqa: F401  (register models on Base.metadata)
from app.db.base import Base

config = context.config

# Skipped when the app runs migrations in-process: it has already configured
# logging, and fileConfig would tear those handlers down.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or app_config.db_url()


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        # SQLite cannot drop a column or change a constraint in place; batch mode
        # rebuilds the table around the change instead. The migrations here rely
        # on it, so it is set for every run rather than per operation.
        render_as_batch=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # An in-process caller passes its own connection, so the migration runs on
    # the engine that is about to serve requests rather than a second one opened
    # behind its back — which on SQLite would be a second writer.
    connection = config.attributes.get("connection", None)
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as conn:
        _configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
