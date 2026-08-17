"""The cupboard, as a page and as two corrections.

Reads decay everything toward the next shop, so what this returns is what the
next basket build will actually spend — the page and the planner cannot
disagree. Writes are the only two things a person can usefully say about a
shelf without weighing anything: "I have run out" and "yes, still there". Both
are believed outright; the model's guesses never outrank a statement.

Per-retailer like the lots themselves, from the active retailer rather than a
path segment — the cupboard page is asking about "my shop", the same way the
basket page does.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, sessionmaker

from app import schedule as sched
from app.api.deps import (
    get_active_retailer,
    get_current_user,
    get_session,
    get_session_factory,
)
from app.api.schedule import cadence_weeks
from app.api.schemas import PantryItemIn, PantryItemOut, PantryOut
from app.db.models import User
from app.pantry import store

router = APIRouter(prefix="/api/pantry", tags=["pantry"])


def _cupboard_out(
    factory: sessionmaker[Session],
    session: Session,
    user: User,
    retailer: str,
) -> PantryOut:
    target_week = sched.format_date(sched.upcoming_week_start())
    items = store.read_cupboard(
        factory,
        user_id=user.id,
        retailer=retailer,
        target_week=target_week,
        cadence_weeks=cadence_weeks(session, user.id),
    )
    return PantryOut(
        items=[PantryItemOut(**item) for item in items], target_week=target_week
    )


@router.get("", response_model=PantryOut)
def cupboard(
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> PantryOut:
    return _cupboard_out(factory, session, user, retailer)


@router.put("/item", response_model=PantryOut)
def set_item(
    body: PantryItemIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> PantryOut:
    """Correct one shelf. The key travels in the body — ingredient keys hold
    slashes, and a path segment would eat them."""
    changed = (
        store.confirm(
            factory,
            user_id=user.id,
            retailer=retailer,
            ingredient_key=body.ingredient_key,
        )
        if body.present
        else store.empty(
            factory,
            user_id=user.id,
            retailer=retailer,
            ingredient_key=body.ingredient_key,
        )
    )
    if not changed:
        raise HTTPException(
            status_code=404,
            detail=f"Nothing in the cupboard for {body.ingredient_key}",
        )
    return _cupboard_out(factory, session, user, retailer)
