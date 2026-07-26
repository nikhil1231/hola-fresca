"""FastAPI dependencies for the API layer.

A single engine/session factory is created for the process (SQLite, local file)
and a fresh session is yielded per request. Tests override ``get_session`` to
point at a temporary database.
"""
from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import ensure_runtime_schema, make_engine, make_session_factory


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    engine = make_engine()
    ensure_runtime_schema(engine)
    return make_session_factory(engine)


def get_session() -> Iterator[Session]:
    factory = _session_factory()
    with factory() as session:
        yield session


def get_session_factory() -> sessionmaker[Session]:
    """Session factory for services that need to own their session lifecycle."""
    return _session_factory()


def get_planner_csv_path() -> Path | None:
    """Ingredient-frequency CSV override hook for planner API tests."""
    return None
