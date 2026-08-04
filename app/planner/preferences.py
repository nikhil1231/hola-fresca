"""One user's standing pack choices, read the way the planner wants them.

Small enough to be its own module because everything that prices anything needs
it — the basket, the browse prices, the suggestion ranking — and none of those
should be reaching into the ORM to work out the shape themselves.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import UserPackPreference
from app.planner.index import RETAILER


def pack_preferences(
    session: Session, user_id: int, *, retailer: str = RETAILER
) -> dict[str, str]:
    """``{ingredient_key: sku}`` for one user.

    Usually empty or nearly so — a standing choice is made once, for the handful
    of things where the big bag is obviously right — which is worth knowing,
    because it is what makes keying the planner's derived caches on this map
    affordable.
    """
    rows = session.execute(
        select(UserPackPreference.ingredient_key, UserPackPreference.sku).where(
            UserPackPreference.user_id == user_id,
            UserPackPreference.retailer == retailer,
        )
    ).all()
    return {key: sku for key, sku in rows}


def set_pack_preference(
    session: Session,
    user_id: int,
    ingredient_key: str,
    sku: str | None,
    *,
    retailer: str = RETAILER,
) -> None:
    """Fix this user's pack for an ingredient, or release it with ``sku=None``.

    Does not commit: the caller owns the transaction, because the API endpoint
    that calls this also has to invalidate the planner's derived caches and the
    two should not be able to disagree about whether the write happened.
    """
    if sku is None:
        session.execute(
            delete(UserPackPreference).where(
                UserPackPreference.user_id == user_id,
                UserPackPreference.retailer == retailer,
                UserPackPreference.ingredient_key == ingredient_key,
            )
        )
        return

    row = session.scalar(
        select(UserPackPreference).where(
            UserPackPreference.user_id == user_id,
            UserPackPreference.retailer == retailer,
            UserPackPreference.ingredient_key == ingredient_key,
        )
    )
    if row is None:
        session.add(
            UserPackPreference(
                user_id=user_id, retailer=retailer, ingredient_key=ingredient_key, sku=sku
            )
        )
    else:
        row.sku = sku
