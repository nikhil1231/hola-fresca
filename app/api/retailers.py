"""Which shops exist, and which one this user is shopping at."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import retailers as retailers_mod
from app import schedule as sched
from app.api.deps import get_active_retailer, get_current_user, get_session
from app.api.schemas import RetailerOut, RetailersOut, RetailerSelectionIn
from app.db.models import PlanSettings, User

router = APIRouter(prefix="/api/retailers", tags=["retailers"])


def _out(active: str) -> RetailersOut:
    return RetailersOut(
        active=active,
        items=[
            RetailerOut(
                id=retailer.id,
                label=retailer.label,
                catalogued=retailer.catalogued,
                shoppable=retailer.shoppable,
            )
            for retailer in retailers_mod.RETAILERS
        ],
    )


@router.get("", response_model=RetailersOut)
def list_retailers(active: str = Depends(get_active_retailer)) -> RetailersOut:
    """Every shop the app knows, and the one this user's weeks are priced at.

    ``shoppable`` is the field the UI branches on: a retailer without it can be
    planned and priced but has no cart to push to, so the basket page offers a
    list to take to the shop instead of a "send to trolley" button.
    """
    return _out(active)


@router.put("", response_model=RetailersOut)
def set_active_retailer(
    body: RetailerSelectionIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> RetailersOut:
    """Switch shops.

    Personal, not global: it writes this user's ``plan_settings`` row and touches
    nothing shared. The catalogue, the mappings and everyone else's weeks are
    unaffected, which is the whole reason the choice lives on the user.
    """
    if not retailers_mod.is_known(body.retailer):
        known = ", ".join(retailers_mod.RETAILER_IDS)
        raise HTTPException(
            status_code=400, detail=f"Unknown retailer {body.retailer!r}; known: {known}"
        )

    row = session.scalar(select(PlanSettings).where(PlanSettings.user_id == user.id))
    if row is None:
        # Same lazy creation as the schedule API: the row is only worth writing
        # once something in it differs from the defaults, and this does.
        row = PlanSettings(
            user_id=user.id,
            anchor_week_start=sched.format_date(sched.upcoming_week_start()),
        )
        session.add(row)
    row.retailer = body.retailer
    session.commit()
    return _out(body.retailer)
