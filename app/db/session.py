"""Engine and session management.

Phase 1 creates the schema directly with ``Base.metadata.create_all``. The
database is a disposable derivative of the raw payload store — it can be dropped
and rebuilt by re-running the normalize stage — so migrations are deferred until
the schema stabilises with the planner/pantry work.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
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


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
