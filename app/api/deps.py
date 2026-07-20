"""FastAPI dependencies for the API layer.

A single engine/session factory is created for the process (SQLite, local file)
and a fresh session is yielded per request. Tests override ``get_session`` to
point at a temporary database.
"""
from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import make_engine, make_session_factory


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return make_session_factory(make_engine())


def get_session() -> Iterator[Session]:
    factory = _session_factory()
    with factory() as session:
        yield session
