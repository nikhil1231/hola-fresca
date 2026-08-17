"""Persistence for the cupboard: reading it, and filling it from a push.

Module functions over a ``sessionmaker``, owning their own session and commit,
in the shape :mod:`app.cart.ledger` established. Nothing here decides anything —
:mod:`app.pantry.model` holds the rules — it only stores and resolves.

Keyed by ``(user_id, retailer, ingredient_key)``, unlike the cart ledger, which
belongs to a retailer account. A cupboard is a household's, and the retailer half
is there because pack sizes and therefore leftovers differ per shop: 500 g of
Ocado rice and 500 g of Sainsbury's rice are the same food but they arrived from
different baskets, and merging them would attribute one shop's remainder to the
other's plan.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import schedule as sched
from app.db.models import PantryLot, PlanWeekPush
from app.pantry import cooks, model
from app.pantry.model import Lot, Quantity
from app.retailers import DEFAULT_RETAILER


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_lot(row: PantryLot) -> Lot:
    return Lot(
        ingredient_key=row.ingredient_key,
        ingredient_name=row.ingredient_name,
        week_start=row.week_start,
        available=Quantity(grams=row.available_g or 0.0, units=row.available_qty),
        salvage=row.salvage or 0.0,
        contributions=model.parse_contributions(row.contributions_json),
        unit_kind=row.unit_kind or "mass",
        emptied=row.emptied_at is not None,
        confirmed_week_start=row.confirmed_week_start,
    )


def _live_rows(
    session: Session, user_id: int, retailer: str, *, before_week: str | None = None
) -> list[PantryLot]:
    """The newest live lot per ingredient, restricted to shops before ``before_week``.

    The restriction is what makes a re-push read the same cupboard as the first
    push: a week's own deposit describes the shelves *after* its shop, so reading
    it back while shopping for that same week would count the shop twice — or,
    once older lots fell away, count nothing at all. One lot per ingredient
    because a newer shop's row for the same key is a fresher measurement of the
    same shelf, not an addition to it.
    """
    query = (
        select(PantryLot)
        .where(
            PantryLot.user_id == user_id,
            PantryLot.retailer == retailer,
            PantryLot.superseded_at.is_(None),
        )
        .order_by(PantryLot.week_start.desc())
    )
    if before_week is not None:
        # A row someone has spoken for is exempt. The restriction guards against
        # a shop's own deposit being spent again inside the week that bought it;
        # a stated figure describes the shelf as it is now, was not derived from
        # any shop, and would otherwise be invisible on the page that set it.
        query = query.where(
            (PantryLot.week_start < before_week) | PantryLot.confirmed_at.is_not(None)
        )
    newest: dict[str, PantryLot] = {}
    for row in session.scalars(query):
        newest.setdefault(row.ingredient_key, row)
    return list(newest.values())


def _resolve(
    session: Session,
    rows: list[PantryLot],
    *,
    user_id: int,
    target_week: str,
    cadence_weeks: int,
    today: date | None = None,
) -> list[tuple[PantryLot, Quantity]]:
    """Pair each live lot with what it currently holds.

    Cooked-ness is resolved per week rather than per lot, since a week's lots all
    share the same answer and the lookup is two queries a week.
    """
    cooked_by_week: dict[str, set[int]] = {}
    resolved: list[tuple[PantryLot, Quantity]] = []
    for row in rows:
        if row.week_start not in cooked_by_week:
            cooked_by_week[row.week_start] = cooks.cooked_recipe_ids(
                session, user_id, row.week_start, today=today
            )
        quantity = model.held(
            _as_lot(row),
            cooked_recipe_ids=cooked_by_week[row.week_start],
            target_week=target_week,
            cadence_weeks=cadence_weeks,
        )
        resolved.append((row, quantity))
    return resolved


def read_pantry(
    factory: sessionmaker[Session],
    *,
    user_id: int,
    retailer: str | None = None,
    target_week: str,
    cadence_weeks: int = 1,
    today: date | None = None,
) -> dict[str, Quantity]:
    """What may be spent against ``target_week``'s demand, per ingredient key.

    The shape :func:`app.planner.basket.build_basket` takes. Negligible holdings
    are dropped here rather than in the basket, so a stray gram never reaches a
    cover decision or a page.
    """
    retailer = retailer or DEFAULT_RETAILER
    with factory() as session:
        resolved = _resolve(
            session,
            _live_rows(session, user_id, retailer, before_week=target_week),
            user_id=user_id,
            target_week=target_week,
            cadence_weeks=cadence_weeks,
            today=today,
        )
    return {
        row.ingredient_key: quantity for row, quantity in resolved if quantity
    }


def read_cupboard(
    factory: sessionmaker[Session],
    *,
    user_id: int,
    retailer: str | None = None,
    target_week: str,
    cadence_weeks: int = 1,
    today: date | None = None,
) -> list[dict]:
    """The same lots, with enough about each to render a page.

    Kept apart from :func:`read_pantry` because the planner wants a lookup and the
    page wants provenance — where it came from and how long it has been believed
    are the two things that make a claim checkable.
    """
    retailer = retailer or DEFAULT_RETAILER
    with factory() as session:
        resolved = _resolve(
            session,
            _live_rows(session, user_id, retailer, before_week=target_week),
            user_id=user_id,
            target_week=target_week,
            cadence_weeks=cadence_weeks,
            today=today,
        )
        out = []
        for row, quantity in resolved:
            if not quantity:
                continue
            lot = _as_lot(row)
            out.append(
                {
                    "ingredient_key": row.ingredient_key,
                    "name": row.ingredient_name or row.ingredient_key,
                    "week_start": row.week_start,
                    "unit_kind": row.unit_kind,
                    "held_g": round(quantity.grams, 1),
                    "held_qty": (
                        round(quantity.units, 2) if quantity.units is not None else None
                    ),
                    "bought_g": round(row.available_g or 0.0, 1),
                    "bought_qty": (
                        round(row.available_qty, 2)
                        if row.available_qty is not None
                        else None
                    ),
                    "salvage": round(row.salvage or 0.0, 2),
                    "cycles_held": model.cycles_between(
                        lot.counts_from, target_week, cadence_weeks=cadence_weeks
                    ),
                    "confirmed_week_start": row.confirmed_week_start,
                }
            )
    return out


def live_salvages(
    factory: sessionmaker[Session],
    *,
    user_id: int,
    retailer: str | None = None,
    before_week: str,
) -> dict[str, float]:
    """Each held ingredient's stored salvage, for lots a new shop carries forward.

    A line covered entirely from the cupboard buys nothing, so its new lot has no
    cover to take a salvage from — the honest figure is the one the food came in
    with.
    """
    retailer = retailer or DEFAULT_RETAILER
    with factory() as session:
        return {
            row.ingredient_key: row.salvage or 0.0
            for row in _live_rows(session, user_id, retailer, before_week=before_week)
        }


def record_push(
    factory: sessionmaker[Session],
    *,
    user_id: int,
    retailer: str | None = None,
    week_start: str,
) -> None:
    """Note that this week's basket reached a real trolley."""
    retailer = retailer or DEFAULT_RETAILER
    with factory() as session:
        row = session.scalar(
            select(PlanWeekPush).where(
                PlanWeekPush.user_id == user_id,
                PlanWeekPush.retailer == retailer,
                PlanWeekPush.week_start == week_start,
            )
        )
        if row is None:
            session.add(
                PlanWeekPush(
                    user_id=user_id,
                    retailer=retailer,
                    week_start=week_start,
                    pushed_at=_utcnow(),
                )
            )
        else:
            row.pushed_at = _utcnow()
        session.commit()


def deposit(
    factory: sessionmaker[Session],
    lots: list[Lot],
    *,
    user_id: int,
    retailer: str | None = None,
    week_start: str,
    cadence_weeks: int = 1,
    today: date | None = None,
) -> None:
    """Record what this week's shop leaves in the cupboard.

    Idempotent in the way that matters: pushing the same week twice overwrites
    the same rows rather than stacking a second copy of the shop on top of the
    first, because ``available`` is what the cupboard *holds* after the push, not
    what the push added to it.

    Earlier lots for the same ingredient are left alone. Reads take the newest
    lot per key from before the week being shopped for, so a fresh deposit
    shadows the old row for every later week without touching it — and a re-push
    of this same week still reads past *it* to the same cupboard the first push
    saw.
    """
    retailer = retailer or DEFAULT_RETAILER
    if not lots:
        return
    with factory() as session:
        for lot in lots:
            existing = session.scalar(
                select(PantryLot).where(
                    PantryLot.user_id == user_id,
                    PantryLot.retailer == retailer,
                    PantryLot.ingredient_key == lot.ingredient_key,
                    PantryLot.week_start == week_start,
                )
            )
            if existing is None:
                existing = PantryLot(
                    user_id=user_id,
                    retailer=retailer,
                    ingredient_key=lot.ingredient_key,
                    week_start=week_start,
                )
                session.add(existing)
            existing.ingredient_name = lot.ingredient_name
            existing.available_g = lot.available.grams
            existing.available_qty = lot.available.units
            existing.unit_kind = lot.unit_kind
            existing.salvage = lot.salvage
            existing.contributions_json = model.dump_contributions(lot.contributions)
            # A re-push is a fresh measurement, so any earlier "I ran out" or
            # "still there" said against this row no longer applies.
            existing.superseded_at = None
            existing.emptied_at = None
            existing.confirmed_at = None
            existing.confirmed_week_start = None
        session.commit()
    prune(factory, user_id=user_id, retailer=retailer, cadence_weeks=cadence_weeks, today=today)


def prune(
    factory: sessionmaker[Session],
    *,
    user_id: int,
    retailer: str | None = None,
    cadence_weeks: int = 1,
    today: date | None = None,
) -> None:
    """Delete lots past the trust horizon.

    Not merely a tidy-up. A belief with no expiry is what lets one 5 kg sack of
    flour suppress flour purchases for a year at a slowly shrinking figure, so
    dropping outright is the point rather than a side effect.
    """
    retailer = retailer or DEFAULT_RETAILER
    with factory() as session:
        rows = session.scalars(
            select(PantryLot).where(
                PantryLot.user_id == user_id, PantryLot.retailer == retailer
            )
        ).all()
        stale = [
            row
            for row in rows
            if model.is_stale(_as_lot(row), today=today, cadence_weeks=cadence_weeks)
        ]
        if not stale:
            return
        for row in stale:
            session.delete(row)
        session.commit()


def _live_row(
    session: Session, *, user_id: int, retailer: str, ingredient_key: str
) -> PantryLot | None:
    return session.scalar(
        select(PantryLot)
        .where(
            PantryLot.user_id == user_id,
            PantryLot.retailer == retailer,
            PantryLot.ingredient_key == ingredient_key,
            PantryLot.superseded_at.is_(None),
        )
        .order_by(PantryLot.week_start.desc())
    )


def empty(
    factory: sessionmaker[Session],
    *,
    user_id: int,
    retailer: str | None = None,
    ingredient_key: str,
) -> bool:
    """"I have run out of this." Believed without qualification."""
    retailer = retailer or DEFAULT_RETAILER
    with factory() as session:
        row = _live_row(
            session, user_id=user_id, retailer=retailer, ingredient_key=ingredient_key
        )
        if row is None:
            return False
        row.emptied_at = _utcnow()
        session.commit()
    return True


def confirm(
    factory: sessionmaker[Session],
    *,
    user_id: int,
    retailer: str | None = None,
    ingredient_key: str,
    week_start: str | None = None,
    cadence_weeks: int = 1,
    today: date | None = None,
) -> bool:
    """"Yes, that is still in the cupboard." Restarts the decay clock.

    Confirming is stating a quantity — the one on the page being confirmed. A
    person clicking "still there" beside "595 g" is vouching for the 595 g, not
    merely for the existence of some rice, so the figure is snapshotted and the
    row becomes an ordinary statement. That keeps one kind of user-touched lot
    rather than two, and means a confirmation cannot later be re-derived into a
    different number than the one that was agreed to.
    """
    retailer = retailer or DEFAULT_RETAILER
    week_start = week_start or sched.format_date(sched.week_start_for(date.today()))
    with factory() as session:
        row = _live_row(
            session, user_id=user_id, retailer=retailer, ingredient_key=ingredient_key
        )
        if row is None:
            return False
        # Read as though it had never been emptied: "no, it is still there" is
        # a retraction of the running-out, so the figure to vouch for is the one
        # that stood before it.
        quantity = model.held(
            replace(_as_lot(row), emptied=False),
            cooked_recipe_ids=cooks.cooked_recipe_ids(
                session, user_id, row.week_start, today=today
            ),
            target_week=week_start,
            cadence_weeks=cadence_weeks,
        )
        name = row.ingredient_name or ingredient_key
        unit_kind = row.unit_kind or "mass"
        salvage = row.salvage or 0.0
    set_quantity(
        factory,
        user_id=user_id,
        retailer=retailer,
        ingredient_key=ingredient_key,
        ingredient_name=name,
        quantity=quantity,
        salvage=salvage,
        unit_kind=unit_kind,
        week_start=week_start,
    )
    return True


def set_quantity(
    factory: sessionmaker[Session],
    *,
    user_id: int,
    retailer: str | None = None,
    ingredient_key: str,
    ingredient_name: str,
    quantity: Quantity,
    salvage: float,
    unit_kind: str = "mass",
    week_start: str | None = None,
) -> None:
    """"There is exactly this much of it." Adds the row, or replaces the guess.

    A stated quantity is the strongest evidence the cupboard ever gets, so it
    supersedes the whole derivation rather than adjusting it: the contributions
    are dropped, because what each of some past week's recipes was going to take
    out of this lot has no bearing on a figure someone has just read off the
    shelf. Confirming it in the current week restarts the decay clock, so the
    amount stated is the amount the next shop sees.

    The same call adds and edits. There is no meaningful difference between
    stating a quantity for something the model had never heard of and stating
    one for something it had guessed at — both are a person overruling it.
    """
    retailer = retailer or DEFAULT_RETAILER
    week_start = week_start or sched.format_date(sched.week_start_for(date.today()))
    now = _utcnow()
    with factory() as session:
        row = _live_row(
            session, user_id=user_id, retailer=retailer, ingredient_key=ingredient_key
        )
        if row is None:
            row = PantryLot(
                user_id=user_id,
                retailer=retailer,
                ingredient_key=ingredient_key,
                week_start=week_start,
            )
            session.add(row)
        row.ingredient_name = ingredient_name
        row.available_g = quantity.grams
        row.available_qty = quantity.units
        row.unit_kind = unit_kind
        row.salvage = salvage
        row.contributions_json = None
        row.superseded_at = None
        row.emptied_at = None
        row.confirmed_at = now
        row.confirmed_week_start = week_start
        session.commit()


def remove(
    factory: sessionmaker[Session],
    *,
    user_id: int,
    retailer: str | None = None,
    ingredient_key: str,
) -> bool:
    """Take an ingredient out of the cupboard entirely.

    Deletes rather than emptying, unlike :func:`empty`. The two say different
    things: "I ran out" is a fact about the food that the record of the shop
    should survive, while removing an entry says the row should not have been
    there at all. Every lot for the key goes, including shadowed ones, so a
    removed ingredient cannot be resurrected by an older shop's row.
    """
    retailer = retailer or DEFAULT_RETAILER
    with factory() as session:
        rows = session.scalars(
            select(PantryLot).where(
                PantryLot.user_id == user_id,
                PantryLot.retailer == retailer,
                PantryLot.ingredient_key == ingredient_key,
            )
        ).all()
        if not rows:
            return False
        for row in rows:
            session.delete(row)
        session.commit()
    return True
