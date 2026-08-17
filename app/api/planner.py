"""Stateless planner API: basket pricing and best-fit recipe ranking."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.api.deps import (
    get_active_retailer,
    get_current_user,
    get_planner_csv_path,
    get_session,
    get_session_factory,
)
from app.api.recipes import (
    _filtered_recipe_ids,
    _personal_rating_map,
    _recipe_ids_with_pricing_gaps,
    _library_condition,
    _uncurated_match_count,
    _wishlist_map,
    _to_card,
)
from app.api.schemas import (
    BasketIn,
    BasketContributionOut,
    BasketLineOut,
    BasketOut,
    BasketPackChoiceOut,
    BasketPackOptionOut,
    BasketSnapOut,
    BasketSubstitutionOut,
    PackPreferenceIn,
    PackPreferenceOut,
    PlannerSuggestionsOut,
    RecipeSuggestionCard,
    StockRefreshOut,
    SuggestionsIn,
)
from app.api.schedule import cadence_weeks, pack_shortfall_tolerance_pct
from app import catalogue
from app.db.models import (
    IngredientMapping,
    Recipe,
    User,
    UserRetailerPriceRefresh,
)
from app.pantry import store as pantry_store
from app.planner.basket import (
    Basket,
    BasketLine,
    Demand,
    Selection,
    build_basket,
)
from app.planner.cache import get_index, get_ranking, note_pack_preference
from app.planner.index import RETAILER, PlanIndex
from app.planner.preferences import pack_preferences, set_pack_preference as write_pack_preference

router = APIRouter(prefix="/api/planner", tags=["planner"])
assert SuggestionsIn.model_fields["candidate_portions"].default == 4
PRICE_REFRESH_COOLDOWN = timedelta(minutes=5)


def _planner_selection(body_selection) -> Selection:
    protein = getattr(body_selection, "protein", None)
    return Selection(
        recipe_id=body_selection.recipe_id,
        servings=body_selection.portions,
        protein=protein.to_domain() if protein is not None else None,
    )


def _selection_ids(selections) -> list[int]:
    return list(dict.fromkeys(s.recipe_id for s in selections))


def _require_curated(session: Session, recipe_ids: list[int]) -> None:
    """Every id must be in the shared library.

    Checked against the library rather than against what the user can see: a
    recipe they have personally hidden is still a legitimate thing to have in a
    plan, and pricing one should not start failing because they hid it after
    choosing it.
    """
    if not recipe_ids:
        return
    found = set(
        session.scalars(
            select(Recipe.id).where(*_library_condition(), Recipe.id.in_(recipe_ids))
        )
    )
    missing = [rid for rid in recipe_ids if rid not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown or unavailable recipe id(s): {', '.join(map(str, missing))}",
        )


def _round_money(value: float) -> float:
    return round(value, 2)


def _utc(value: datetime | None) -> datetime | None:
    """Stamp UTC onto a timestamp SQLite handed back naive.

    Everything written is ``datetime.now(timezone.utc)``, but the SQLite DATETIME
    type drops the offset on the way in. Serialised naive, a browser reads the
    result as local time and a freshly checked basket looks hours old.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _stock_checked_at(basket: Basket) -> datetime | None:
    """How fresh the *stalest* pack in the basket is - what the basket can claim."""
    stamps = [
        choice.pack.stock_checked_at
        for line in basket.lines
        if line.cover is not None
        for choice in line.cover.choices
        if not choice.pack.external
    ]
    if not stamps or any(stamp is None for stamp in stamps):
        return None
    return _utc(min(stamps))


def _option_out(option) -> BasketPackOptionOut:
    # Per kilo rather than per gram: £0.0025/g is a number nobody can shop with.
    scale = 1.0 if option.quantity_unit == "unit" else 1000.0
    return BasketPackOptionOut(
        sku=option.pack.sku,
        product_name=option.pack.product_name,
        pack_size_raw=option.pack.pack_size_raw,
        url=option.pack.url,
        count=option.count,
        cost=_round_money(option.cost),
        capacity=round(option.capacity, 3 if option.quantity_unit == "unit" else 1),
        leftover=round(option.leftover, 3 if option.quantity_unit == "unit" else 1),
        unit_cost=_round_money(option.unit_cost * scale),
        cost_delta=_round_money(option.cost_delta),
        leftover_delta=round(option.leftover_delta, 3 if option.quantity_unit == "unit" else 1),
        quantity_unit=option.quantity_unit,
        keeps=option.keeps,
        chosen=option.chosen,
        pinned=option.pinned,
        this_week=option.this_week,
        better_value=option.better_value,
        match_type=option.pack.match_type,
        form_differs=option.form_differs,
        is_frozen=option.pack.is_frozen,
        shortfall=round(option.shortfall, 3 if option.quantity_unit == "unit" else 1),
        shortfall_pct=round(option.shortfall_pct, 1),
        recommended=option.recommended,
        recommendation_reason=option.recommendation_reason,
        rating=option.pack.rating,
        ratings_count=option.pack.ratings_count,
        weeks_of_supply=(
            round(option.supply.weeks, 1) if option.supply is not None else None
        ),
        supply_limited_by=option.supply.limited_by if option.supply is not None else None,
    )


def _substitution_out(line: BasketLine) -> BasketSubstitutionOut | None:
    substitution = line.substitution
    if substitution is None:
        return None
    return BasketSubstitutionOut(
        displaced=list(substitution.displaced),
        displaced_skus=list(substitution.displaced_skus),
        baseline_cost=_round_money(substitution.baseline_cost),
        cost_delta=_round_money(substitution.cost_delta),
        tier_changed=substitution.tier_changed,
    )


def _line_out(line: BasketLine) -> BasketLineOut:
    cover = line.cover
    choices = []
    if cover is not None:
        choices = [
            BasketPackChoiceOut(
                sku=choice.pack.sku,
                product_name=choice.pack.product_name,
                pack_size_raw=choice.pack.pack_size_raw,
                url=choice.pack.url,
                capacity_g=round(choice.pack.capacity_g, 1),
                capacity_qty=round(choice.pack.capacity_qty, 3) if choice.pack.capacity_qty is not None else None,
                quantity_unit=choice.pack.quantity_unit,
                price=_round_money(choice.pack.price),
                count=choice.count,
                cost=_round_money(choice.cost),
                retailer=choice.pack.retailer,
                external=choice.pack.external,
                is_nectar_price=choice.pack.is_nectar_price,
                is_frozen=choice.pack.is_frozen,
            )
            for choice in cover.choices
        ]
    return BasketLineOut(
        key=line.key,
        name=line.name,
        need_g=line.need_g,
        need_qty=line.need_qty,
        quantity_unit=line.quantity_unit,
        capacity_g=round(cover.capacity_g, 1) if cover else None,
        capacity_qty=round(cover.capacity_qty, 3) if cover and cover.capacity_qty is not None else None,
        leftover_g=round(cover.leftover_g, 1) if cover else None,
        leftover_qty=round(cover.leftover_qty, 3) if cover and cover.leftover_qty is not None else None,
        cost=_round_money(line.cost),
        waste_gbp=_round_money(line.waste_gbp),
        score=_round_money(cover.score if cover else 0.0),
        packs=cover.packs if cover else 0,
        trace=line.trace,
        external=line.external,
        is_nectar_price=any(choice.is_nectar_price for choice in choices),
        note=line.note,
        substitution=_substitution_out(line),
        options=[_option_out(option) for option in line.options],
        choices=choices,
        pantry_g=line.pantry_g,
        pantry_qty=line.pantry_qty,
        contributions=[
            BasketContributionOut(
                recipe_id=contribution.recipe_id,
                recipe_name=contribution.recipe_name,
                grams=round(contribution.grams, 1),
                quantity=round(contribution.quantity, 3) if contribution.quantity is not None else None,
                quantity_unit=contribution.quantity_unit,
            )
            for contribution in line.contributions
        ],
        snap=(
            BasketSnapOut(
                original_need_g=round(line.snap.original_need_g, 1),
                snapped_need_g=round(line.snap.snapped_need_g, 1),
                reduction_pct=round(line.snap.reduction_pct, 1),
                saving_gbp=_round_money(line.snap.saving_gbp),
            )
            if line.snap else None
        ),
        snapped=line.snapped,
    )


def _basket_out(basket: Basket) -> BasketOut:
    return BasketOut(
        lines=[_line_out(line) for line in basket.lines],
        staples=basket.staples,
        unmapped=basket.unmapped,
        unpriceable=basket.unpriceable,
        sold_out=basket.sold_out,
        untracked_lines=basket.untracked_lines,
        cost=_round_money(basket.cost),
        waste_gbp=_round_money(basket.waste_gbp),
        score=_round_money(basket.score),
        stock_checked_at=_stock_checked_at(basket),
    )


def _load_planner_index(
    factory: sessionmaker[Session],
    recipe_ids: list[int],
    csv_path: Path | None,
    retailer: str = RETAILER,
) -> PlanIndex:
    """Hydrate exactly the recipes the basket is about to use."""
    return get_index(
        factory, recipe_ids=recipe_ids, csv_path=csv_path, retailer=retailer
    )


def _pantry_demands(
    factory: sessionmaker[Session],
    session: Session,
    user: User,
    retailer: str,
    week_start: str | None,
) -> dict[str, Demand]:
    """The cupboard as :func:`build_basket` spends it, or nothing without a week.

    Gated on the week label for the same reason the push is: a basket priced for
    no week in particular cannot say which shop's cupboard it would be spending.
    """
    if not week_start:
        return {}
    held = pantry_store.read_pantry(
        factory,
        user_id=user.id,
        retailer=retailer,
        target_week=week_start,
        cadence_weeks=cadence_weeks(session, user.id),
    )
    return {
        key: Demand(grams=quantity.grams, units=quantity.units)
        for key, quantity in held.items()
    }


@router.post("/basket", response_model=BasketOut)
def basket(
    body: BasketIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> BasketOut:
    recipe_ids = _selection_ids(body.selections)
    _require_curated(session, recipe_ids)
    if not body.selections:
        return _basket_out(Basket())

    index = _load_planner_index(factory, recipe_ids, csv_path, retailer)
    selections = [_planner_selection(selection) for selection in body.selections]
    return _basket_out(build_basket(
        index,
        selections,
        pack_overrides=body.pack_overrides,
        snap_overrides=body.snap_overrides,
        pack_preferences=pack_preferences(session, user.id, retailer=retailer),
        pack_shortfall_tolerance_pct=pack_shortfall_tolerance_pct(session, user.id),
        pantry=_pantry_demands(factory, session, user, retailer, body.week_start),
    ))


@router.post("/stock/refresh", response_model=StockRefreshOut)
def stock_refresh(
    body: BasketIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> StockRefreshOut:
    """Re-read stock and price for everything this basket could be covered from.

    Everything, not just the packs it chose: a substitute is only reachable if
    its stock is known, and one marked sold out weeks ago never comes back
    without being asked again. Checking the whole shortlist is what lets the
    planner move between them — and what makes the prices behind the total the
    prices being charged today, promotions included.

    Lives here rather than under ``/ocado`` because the answer depends on which
    shop the basket is priced at, not on being signed in to any of them.
    """
    if not catalogue.supports_live_status(retailer):
        raise HTTPException(
            status_code=501, detail=f"{retailer} has no live stock and price check"
        )
    recipe_ids = _selection_ids(body.selections)
    _require_curated(session, recipe_ids)
    selections = [_planner_selection(selection) for selection in body.selections]
    index = _load_planner_index(factory, recipe_ids, csv_path, retailer)
    basket = build_basket(
        index,
        selections,
        pack_overrides=body.pack_overrides,
        snap_overrides=body.snap_overrides,
        pack_preferences=pack_preferences(session, user.id, retailer=retailer),
        pack_shortfall_tolerance_pct=pack_shortfall_tolerance_pct(session, user.id),
    )
    reserved_at = datetime.now(timezone.utc)
    performed, previous_refresh = _reserve_price_refresh(
        session, user.id, retailer, reserved_at
    )
    if not performed:
        checked_at = _utc(previous_refresh)
        assert checked_at is not None
        return StockRefreshOut(
            checked_at=checked_at,
            performed=False,
            next_refresh_at=checked_at + PRICE_REFRESH_COOLDOWN,
        )
    try:
        result = catalogue.refresh_stock(
            factory, candidate_skus(index, basket), retailer=retailer
        )
    except Exception as exc:  # noqa: BLE001 - the shop's failure, not the app's
        _restore_price_refresh(
            session, user.id, retailer, reserved_at, previous_refresh
        )
        raise HTTPException(
            status_code=502, detail=f"{retailer} stock check failed: {exc}"
        ) from exc
    return StockRefreshOut(
        checked_at=result.checked_at,
        performed=True,
        next_refresh_at=reserved_at + PRICE_REFRESH_COOLDOWN,
        checked=result.checked,
        available=result.available,
        sold_out=result.sold_out,
        restocked=result.restocked,
        repriced=result.repriced,
        changed=result.changed,
    )


def _reserve_price_refresh(
    session: Session, user_id: int, retailer: str, now: datetime
) -> tuple[bool, datetime | None]:
    """Atomically claim this user's retailer refresh window.

    SQLite is the application's database, so its conflict-aware insert gives a
    new user/retailer pair the same one-winner guarantee as the conditional
    update used for an existing pair.
    """
    inserted = session.execute(
        sqlite_insert(UserRetailerPriceRefresh)
        .values(user_id=user_id, retailer=retailer, last_refreshed_at=now)
        .on_conflict_do_nothing(index_elements=["user_id", "retailer"])
    )
    if inserted.rowcount:
        session.commit()
        return True, None

    row = session.scalar(
        select(UserRetailerPriceRefresh).where(
            UserRetailerPriceRefresh.user_id == user_id,
            UserRetailerPriceRefresh.retailer == retailer,
        )
    )
    if row is None:  # Defensive: another transaction removed it between reads.
        session.rollback()
        return _reserve_price_refresh(session, user_id, retailer, now)
    previous = row.last_refreshed_at
    claimed = session.execute(
        update(UserRetailerPriceRefresh)
        .where(
            UserRetailerPriceRefresh.id == row.id,
            UserRetailerPriceRefresh.last_refreshed_at <= now - PRICE_REFRESH_COOLDOWN,
        )
        .values(last_refreshed_at=now)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    if claimed.rowcount:
        return True, previous
    current = session.scalar(
        select(UserRetailerPriceRefresh.last_refreshed_at).where(
            UserRetailerPriceRefresh.id == row.id
        )
    )
    return False, current


def _restore_price_refresh(
    session: Session,
    user_id: int,
    retailer: str,
    reserved_at: datetime,
    previous: datetime | None,
) -> None:
    """Release a failed reservation without undoing a newer successful claim."""
    filters = (
        UserRetailerPriceRefresh.user_id == user_id,
        UserRetailerPriceRefresh.retailer == retailer,
        UserRetailerPriceRefresh.last_refreshed_at == reserved_at,
    )
    if previous is None:
        session.execute(
            delete(UserRetailerPriceRefresh)
            .where(*filters)
            .execution_options(synchronize_session=False)
        )
    else:
        session.execute(
            update(UserRetailerPriceRefresh)
            .where(*filters)
            .values(last_refreshed_at=previous)
            .execution_options(synchronize_session=False)
        )
    session.commit()


def candidate_skus(index: PlanIndex, basket: Basket) -> list[str]:
    """Every product the basket's ingredients are allowed to be covered from.

    Not just the packs it chose: a substitute is only reachable if its stock is
    known, and one marked sold out weeks ago never comes back without being
    asked again. Checking the whole shortlist is what lets the planner move
    between them.
    """
    skus: list[str] = []
    keys = {line.key for line in basket.lines}
    for key in sorted(keys):
        ingredient = index.ingredient(key)
        if ingredient is None:
            continue
        skus.extend(pack.sku for pack in ingredient.packs if not pack.external)
    return list(dict.fromkeys(skus))


@router.put("/preferences/pack", response_model=PackPreferenceOut)
def set_pack_preference(
    body: PackPreferenceIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> PackPreferenceOut:
    """Fix (or release) the pack size this user always buys an ingredient in.

    A standing decision rather than a weekly one - having settled that you buy
    rice by the kilo, the planner should not put it back to the 500 g bag every
    Monday - but yours rather than the ingredient's, so it is stored against the
    account and not on the mapping everyone shares.

    The mapping is still what the choice is checked against: you may only pin a
    pack that is an approved product for the ingredient.
    """
    mapping = session.scalar(
        select(IngredientMapping).where(
            IngredientMapping.retailer == retailer,
            IngredientMapping.ingredient_key == body.ingredient_key,
        )
    )
    if mapping is None:
        raise HTTPException(status_code=404, detail=f"Unknown ingredient: {body.ingredient_key}")
    if body.sku is not None and not any(
        product.sku == body.sku and product.accepted for product in mapping.products
    ):
        raise HTTPException(
            status_code=400,
            detail=f"{body.sku} is not an approved product for {body.ingredient_key}",
        )
    write_pack_preference(
        session, user.id, body.ingredient_key, body.sku, retailer=retailer
    )
    session.commit()
    note_pack_preference(factory)
    return PackPreferenceOut(ingredient_key=body.ingredient_key, sku=body.sku)


def _candidate_ids(
    session: Session,
    body: SuggestionsIn,
    pinned_ids: set[int],
    factory: sessionmaker[Session],
    csv_path: Path | None,
    user_id: int,
    retailer: str = RETAILER,
) -> set[int]:
    """Which recipes the request's filters allow — a membership test, not an order.

    The ranking is computed over the whole library because it does not depend on
    any of this; the filters only decide which of the ranked recipes are shown.
    """
    filters = body.filters.model_dump()
    candidate_ids = set(_filtered_recipe_ids(session, filters, user_id)) - pinned_ids
    if "unmapped" not in body.filters.exclude:
        return candidate_ids

    gap_recipe_ids = _recipe_ids_with_pricing_gaps(
        sorted(candidate_ids), factory, csv_path, retailer
    )
    return candidate_ids - gap_recipe_ids


@router.post("/suggestions", response_model=PlannerSuggestionsOut)
def suggestions(
    body: SuggestionsIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
    user: User = Depends(get_current_user),
    retailer: str = Depends(get_active_retailer),
) -> PlannerSuggestionsOut:
    pinned_recipe_ids = _selection_ids(body.selections)
    _require_curated(session, pinned_recipe_ids)

    pinned = [_planner_selection(selection) for selection in body.selections]
    # Ranked over the whole library and cached against the pinned week, so paging
    # and filter changes are a walk over a list rather than a re-scoring of it.
    # "What would adding this cost me" has to be priced the way the basket will
    # price it, so the ranking carries the asking user's standing pack choices.
    ranked = get_ranking(
        factory,
        pinned,
        candidate_portions=body.candidate_portions,
        csv_path=csv_path,
        retailer=retailer,
        pack_preferences=pack_preferences(session, user.id, retailer=retailer),
    )
    allowed = _candidate_ids(
        session, body, set(pinned_recipe_ids), factory, csv_path, user.id, retailer
    )

    page_scores = [c for c in ranked if c.recipe_id in allowed]
    total = len(page_scores)
    start = body.offset if body.offset is not None else (body.page - 1) * body.page_size
    page_scores = page_scores[start:start + body.page_size]
    page_ids = [c.recipe_id for c in page_scores]
    by_id: dict[int, Recipe] = {}
    if page_ids:
        rows = session.scalars(
            select(Recipe)
            .where(Recipe.id.in_(page_ids))
            .options(selectinload(Recipe.cuisines), selectinload(Recipe.tags))
        ).all()
        by_id = {recipe.id: recipe for recipe in rows}

    personal_ratings = _personal_rating_map(session, user.id, page_ids)
    wishlist = _wishlist_map(session, user.id, page_ids)
    items = []
    for candidate in page_scores:
        recipe = by_id.get(candidate.recipe_id)
        if recipe is None:
            continue
        card = _to_card(
            recipe,
            personal_rating=personal_ratings.get(recipe.id),
            wishlisted=wishlist.get(recipe.id, False),
        ).model_dump()
        available = candidate.available
        items.append(
            RecipeSuggestionCard(
                **card,
                marginal_score=_round_money(candidate.marginal) if available else None,
                standalone_score=(
                    _round_money(candidate.standalone)
                    if candidate.standalone is not None
                    else None
                ),
                ranking_score=_round_money(candidate.ranking) if available else None,
                marginal_cost=(
                    _round_money(candidate.marginal_cost)
                    if candidate.marginal_cost is not None
                    else None
                ),
                standalone_cost=(
                    _round_money(candidate.standalone_cost)
                    if candidate.standalone_cost is not None
                    else None
                ),
                unpriced_gap_count=candidate.gap_count,
                shared_ingredient_count=candidate.shared,
                basket_available=available,
            )
        )

    next_offset = start + len(page_scores)
    has_more = next_offset < total
    return PlannerSuggestionsOut(
        items=items,
        total=total,
        page=body.page,
        page_size=body.page_size,
        has_more=has_more,
        next_offset=next_offset if has_more else None,
        # Best fit ranks the library and nothing else — an uncurated recipe has
        # no marginal cost against a week it cannot be priced into, so widening
        # this endpoint is meaningless. Counting is not: when a search finds
        # nothing here, the honest answer is still "not in your library", and the
        # client turns that into an offer to run a plain search instead.
        uncurated_total=(
            _uncurated_match_count(session, body.filters.model_dump(), user.id)
            if total == 0
            else None
        ),
    )
