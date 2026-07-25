"""Engine and session management.

Phase 1 creates the schema directly with ``Base.metadata.create_all``. The
database is a disposable derivative of the raw payload store — it can be dropped
and rebuilt by re-running the normalize stage — so migrations are deferred until
the schema stabilises with the planner/pantry work.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import config
from app.db.base import Base
from app.db import models  # noqa: F401  (register models on Base.metadata)


def make_engine(db_path: Path | None = None) -> Engine:
    engine = create_engine(config.db_url(db_path), future=True)
    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(recipe_ingredients)"))}
        if "position" not in existing:
            conn.execute(text("ALTER TABLE recipe_ingredients ADD COLUMN position INTEGER"))


def ensure_columns(session: Session, table: str, columns: dict[str, str]) -> None:
    """Add any missing columns to an existing table, in place.

    ``create_all`` only creates whole tables, so an already-populated database
    never gains a newly declared column. Maps column name -> SQLite declaration.
    """
    existing = {row[1] for row in session.execute(text(f"PRAGMA table_info({table})"))}
    for name, decl in columns.items():
        if name not in existing:
            session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))
    session.commit()


def ensure_runtime_schema(engine: Engine) -> None:
    """Keep existing local SQLite DBs compatible with newly declared columns."""
    init_db(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
