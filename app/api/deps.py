"""FastAPI dependencies for the API layer.

A single engine/session factory is created for the process (SQLite, local file)
and a fresh session is yielded per request. Tests override ``get_session`` to
point at a temporary database.
"""
from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import retailers
from app.db.models import User
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


def get_current_user(session: Session = Depends(get_session)) -> User:
    """Whose data this request is about.

    There is no login yet, so this is the account the app bootstrapped — the
    lowest user id, which on any existing database is the person who has been
    running it. Every personal read and write goes through here rather than
    reaching for "the one row", so adding Google sign-in is a change to this
    function and nothing else.
    """
    user = session.scalar(select(User).order_by(User.id).limit(1))
    if user is None:
        # init_db creates this row, so its absence means the API is pointed at a
        # database nothing has initialised — worth saying plainly rather than
        # failing later on a foreign key.
        raise HTTPException(status_code=500, detail="No user account exists in this database")
    return user


def get_active_retailer(
    session: Session = Depends(get_session), user: User = Depends(get_current_user)
) -> str:
    """Which shop this request is about.

    The companion to :func:`get_current_user`: that one answers *whose* data,
    this one answers *where* they shop, and together they are what every priced
    read needs. Endpoints depend on this rather than reaching for a constant, so
    the catalogue, the mappings and the basket all move together when the
    setting changes.

    Deliberately a read, never a write. ``plan_settings`` is created lazily by
    the schedule API when someone first changes something, and a page load is
    not a change — so a user who has never opened settings gets the default here
    without a row appearing. An unrecognised stored value degrades to the default
    too: a retired retailer should not turn every basket into a 500.
    """
    # Imported here rather than at module scope: app.db.models is already loaded
    # by this module, but PlanSettings is only needed on this path.
    from app.db.models import PlanSettings

    stored = session.scalar(
        select(PlanSettings.retailer).where(PlanSettings.user_id == user.id)
    )
    return retailers.resolve(stored)


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Gate for catalogue writes — mapping review, manual products, audits.

    The recipe library, the product cache and the ingredient mappings are shared
    by everyone, so editing them is not a personal act: one person rejecting a
    mapping changes what every other user's basket buys. Today the single user is
    the admin and this never refuses, which is the point of putting it in now —
    the endpoints are already marked, so opening the app up does not mean going
    back through them deciding which were safe.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="This needs an admin account")
    return user


def get_planner_csv_path() -> Path | None:
    """Ingredient-frequency CSV override hook for planner API tests."""
    return None
