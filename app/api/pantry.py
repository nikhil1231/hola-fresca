"""The cupboard: what is in it, and the statements a person can make about it.

Reads decay everything toward the next shop, so what this returns is what the
next basket build will actually spend — the page and the planner cannot
disagree.

Writes are all the same kind of thing: a person overruling the model. "I have
run out", "yes, still there" and "there is exactly 400 g of it" differ only in
how much they say, and the last of them is also how an ingredient the model had
never heard of gets added. Every one of them is believed outright; the model's
guesses never outrank a statement.

Per-retailer like the lots themselves, from the active retailer rather than a
path segment — the cupboard page is asking about "my shop", the same way the
basket page does.
"""
from __future__ import annotations

from pathlib import Path

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import schedule as sched
from app.api.deps import (
    get_active_retailer,
    get_current_user,
    get_planner_csv_path,
    get_session,
    get_session_factory,
)
from app.api.schedule import cadence_weeks
from app.api.schemas import (
    PantryIngredientOut,
    PantryIngredientsOut,
    PantryItemIn,
    PantryItemOut,
    PantryOut,
)
from app.db.models import IngredientMapping, User
from app.pantry import store
from app.pantry.model import PANTRY_MIN_SALVAGE, Quantity
from app.planner import waste
from app.planner.cache import get_index

router = APIRouter(prefix="/api/pantry", tags=["pantry"])

#: How many ingredients the add box offers at once.
SEARCH_LIMIT = 20


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
    # Alphabetical. Neither of the alternatives means anything to a reader: the
    # quantities are in different units and so do not compare, and the shop a
    # lot came from says when it was last measured rather than how much use it
    # is. A name is what someone standing at a cupboard door is looking for.
    items.sort(key=lambda item: item["name"].casefold())
    return PantryOut(
        items=[PantryItemOut(**item) for item in items], target_week=target_week
    )


@dataclass(frozen=True, slots=True)
class _Facts:
    """What the planner knows about an ingredient that the cupboard needs."""

    unit_kind: str
    salvage: float
    each_to_grams: float | None = None

    @property
    def perishable(self) -> bool:
        """Keeps badly enough that a date beats the curve."""
        return self.salvage < PANTRY_MIN_SALVAGE


def _require_date(value: str | None) -> str | None:
    """A ``YYYY-MM-DD`` use-by, or ``None``. Anything else is a client bug."""
    if not value:
        return None
    try:
        return sched.format_date(sched.parse_date(value))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Not a date: {value}") from None


def _ingredient_facts(
    factory: sessionmaker[Session],
    csv_path: Path | None,
    retailer: str,
    key: str,
) -> _Facts | None:
    """An ingredient as the planner sees it, or ``None`` if this shop has no such thing.

    Salvage is read off the real packs rather than guessed, so a hand-added
    entry ages on the same curve as one a shop left behind — and, where that
    curve is the wrong shape, is the signal to ask for a date instead.
    """
    index = get_index(factory, recipe_ids=[], csv_path=csv_path, retailer=retailer)
    ingredient = index.ingredient(key)
    if ingredient is None:
        return None
    packs = ingredient.available_packs or ingredient.packs
    salvage = (
        sum(pack.salvage for pack in packs) / len(packs)
        if packs
        else waste.SALVAGE_UNKNOWN
    )
    return _Facts(
        unit_kind=ingredient.unit_kind,
        salvage=salvage,
        each_to_grams=ingredient.each_to_grams,
    )


@router.get("", response_model=PantryOut)
def cupboard(
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> PantryOut:
    return _cupboard_out(factory, session, user, retailer)


@router.get("/ingredients", response_model=PantryIngredientsOut)
def ingredients(
    q: str = Query(default="", description="what to search the names for"),
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> PantryIngredientsOut:
    """Ingredients that can be put in the cupboard, for the add box.

    Approved mappings at the active shop, minus the staples — those are assumed
    owned outright and the basket never buys them, so tracking a quantity of
    salt would be answering a question nobody asked.
    """
    query = select(IngredientMapping).where(
        IngredientMapping.retailer == retailer,
        IngredientMapping.status == "approved",
        IngredientMapping.pantry_staple == 0,
    )
    if q.strip():
        query = query.where(IngredientMapping.name.ilike(f"%{q.strip()}%"))
    rows = session.scalars(
        query.order_by(func.lower(IngredientMapping.name)).limit(SEARCH_LIMIT)
    ).all()

    target_week = sched.format_date(sched.upcoming_week_start())
    already = set(
        store.read_pantry(
            factory,
            user_id=user.id,
            retailer=retailer,
            target_week=target_week,
            cadence_weeks=cadence_weeks(session, user.id),
        )
    )
    out = []
    for row in rows:
        facts = _ingredient_facts(
            factory, csv_path, retailer, row.ingredient_key
        ) or _Facts(unit_kind=row.unit_kind, salvage=waste.SALVAGE_UNKNOWN)
        out.append(
            PantryIngredientOut(
                ingredient_key=row.ingredient_key,
                name=row.name,
                unit_kind=facts.unit_kind,
                salvage=round(facts.salvage, 2),
                perishable=facts.perishable,
                held=row.ingredient_key in already,
            )
        )
    return PantryIngredientsOut(items=out)


@router.put("/item", response_model=PantryOut)
def set_item(
    body: PantryItemIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> PantryOut:
    """State something about one shelf.

    The key travels in the body rather than the path: ingredient keys hold
    slashes, and a path segment would eat them.
    """
    key = body.ingredient_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="An ingredient key is required")

    stated = body.grams is not None or body.qty is not None
    if stated:
        facts = _ingredient_facts(factory, csv_path, retailer, key)
        if facts is None:
            raise HTTPException(
                status_code=404, detail=f"{retailer} has no ingredient {key}"
            )
        row = session.scalar(
            select(IngredientMapping).where(
                IngredientMapping.retailer == retailer,
                IngredientMapping.ingredient_key == key,
            )
        )
        use_by = _require_date(body.use_by)
        if facts.unit_kind == "count":
            units = float(body.qty or 0.0)
            # Grams alongside the count, so a count lot is not stored as weighing
            # nothing: the figures are two views of one shelf, and the display
            # and the harvest both read the gram one.
            quantity = Quantity(
                grams=units * (facts.each_to_grams or 0.0), units=units
            )
        else:
            quantity = Quantity(grams=body.grams or 0.0)
        if not quantity:
            # Stating nothing is stating it has gone, which is what the page's
            # own remove control says more plainly.
            store.remove(factory, user_id=user.id, retailer=retailer, ingredient_key=key)
            return _cupboard_out(factory, session, user, retailer)
        store.set_quantity(
            factory,
            user_id=user.id,
            retailer=retailer,
            ingredient_key=key,
            ingredient_name=row.name if row is not None else key,
            quantity=quantity,
            salvage=facts.salvage,
            unit_kind=facts.unit_kind,
            use_by=use_by,
        )
        return _cupboard_out(factory, session, user, retailer)

    if body.present is None:
        raise HTTPException(
            status_code=400, detail="Say either how much there is, or whether it is there"
        )
    changed = (
        store.confirm(
            factory, user_id=user.id, retailer=retailer, ingredient_key=key
        )
        if body.present
        else store.empty(
            factory, user_id=user.id, retailer=retailer, ingredient_key=key
        )
    )
    if not changed:
        raise HTTPException(
            status_code=404, detail=f"Nothing in the cupboard for {key}"
        )
    return _cupboard_out(factory, session, user, retailer)


@router.delete("/item", response_model=PantryOut)
def remove_item(
    ingredient_key: str = Query(description="which shelf to clear"),
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> PantryOut:
    """Take an ingredient out of the cupboard entirely.

    Distinct from "I ran out": that is a fact about the food and leaves the
    record of the shop that bought it, while this says the entry should not be
    there at all.
    """
    if not store.remove(
        factory, user_id=user.id, retailer=retailer, ingredient_key=ingredient_key
    ):
        raise HTTPException(
            status_code=404, detail=f"Nothing in the cupboard for {ingredient_key}"
        )
    return _cupboard_out(factory, session, user, retailer)
