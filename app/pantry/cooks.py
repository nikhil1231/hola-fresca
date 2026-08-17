"""Which of a past week's recipes were cooked.

Optimistic by design. Asking someone to tick off five dinners every Sunday is a
chore they will do twice, and a pantry fed by a chore nobody does is worse than
no pantry — so the default is that a recipe still sitting in the plan when its
week ended was made.

The assumption is gated on one piece of evidence: the basket was pushed for that
week. A push is a deliberate act that fills a real trolley, and it is the last
observable point in the chain — nothing here watches a delivery arrive. A week
that was planned and then abandoned pushes nothing, so it reads as "not shopped"
and its recipes consume nothing, which is what stops an idle fortnight quietly
emptying a cupboard that was never filled.

The push is looked up across every retailer. Which shop the ingredients came
from has no bearing on whether the dish got made.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schedule as sched
from app.db.models import PlanCookMark, PlanSelection, PlanWeekPush


def shopped_weeks(session: Session, user_id: int, week_starts: list[str]) -> set[str]:
    """Of the weeks asked about, those with a push on record."""
    if not week_starts:
        return set()
    return set(
        session.scalars(
            select(PlanWeekPush.week_start).where(
                PlanWeekPush.user_id == user_id,
                PlanWeekPush.week_start.in_(week_starts),
            )
        )
    )


def was_shopped(session: Session, user_id: int, week_start: str) -> bool:
    return bool(shopped_weeks(session, user_id, [week_start]))


def cooked_by_default(week_start: str, *, shopped: bool, today: date | None = None) -> bool:
    """The assumption, before any correction: the week ended and it was shopped for."""
    try:
        parsed = sched.parse_date(week_start)
    except ValueError:
        return False
    return shopped and sched.is_complete(parsed, today)


def marks(session: Session, user_id: int, week_start: str) -> dict[int, bool]:
    """``{recipe_id: cooked}`` for the corrections this user has made."""
    rows = session.scalars(
        select(PlanCookMark).where(
            PlanCookMark.user_id == user_id, PlanCookMark.week_start == week_start
        )
    ).all()
    return {row.recipe_id: bool(row.cooked) for row in rows}


def cooked_recipe_ids(
    session: Session, user_id: int, week_start: str, *, today: date | None = None
) -> set[int]:
    """The recipes a lot from ``week_start`` should have had taken out of it.

    Resolves the optimistic default against any :class:`PlanCookMark` rows, over
    the recipes actually selected for that week.
    """
    selected = set(
        session.scalars(
            select(PlanSelection.recipe_id).where(
                PlanSelection.user_id == user_id, PlanSelection.week_start == week_start
            )
        )
    )
    if not selected:
        return set()
    default = cooked_by_default(
        week_start, shopped=was_shopped(session, user_id, week_start), today=today
    )
    overrides = marks(session, user_id, week_start)
    return {
        recipe_id
        for recipe_id in selected
        if overrides.get(recipe_id, default)
    }
