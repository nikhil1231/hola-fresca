"""Stateless planner API: basket pricing and best-fit recipe ranking."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.api.deps import get_planner_csv_path, get_session, get_session_factory
from app.api.recipes import (
    _filtered_recipe_ids,
    _personal_rating_map,
    _recipe_ids_with_pricing_gaps,
    _to_card,
)
from app.api.schemas import (
    BasketIn,
    BasketContributionOut,
    BasketLineOut,
    BasketOut,
    BasketPackChoiceOut,
    PlannerSuggestionsOut,
    RecipeSuggestionCard,
    SuggestionsIn,
)
from app.db.models import Recipe
from app.planner.basket import (
    Basket,
    BasketLine,
    Selection,
    build_basket,
)
from app.planner.cache import get_index, get_ranking
from app.planner.index import PlanIndex

router = APIRouter(prefix="/api/planner", tags=["planner"])
assert SuggestionsIn.model_fields["candidate_portions"].default == 4


def _planner_selection(body_selection) -> Selection:
    return Selection(recipe_id=body_selection.recipe_id, servings=body_selection.portions)


def _selection_ids(selections) -> list[int]:
    return list(dict.fromkeys(s.recipe_id for s in selections))


def _require_curated(session: Session, recipe_ids: list[int]) -> None:
    if not recipe_ids:
        return
    found = set(
        session.scalars(
            select(Recipe.id).where(Recipe.curated == 1, Recipe.id.in_(recipe_ids))
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
        note=line.note,
        choices=choices,
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
    )


def _basket_out(basket: Basket) -> BasketOut:
    return BasketOut(
        lines=[_line_out(line) for line in basket.lines],
        staples=basket.staples,
        unmapped=basket.unmapped,
        unpriceable=basket.unpriceable,
        untracked_lines=basket.untracked_lines,
        cost=_round_money(basket.cost),
        waste_gbp=_round_money(basket.waste_gbp),
        score=_round_money(basket.score),
    )


def _load_planner_index(
    factory: sessionmaker[Session],
    recipe_ids: list[int],
    csv_path: Path | None,
) -> PlanIndex:
    """The shared curated index; ``recipe_ids`` is only ever a subset of it.

    Every recipe id reaching this module has been through ``_require_curated`` or
    ``_apply_filters``, both of which insist on the curated library.
    """
    return get_index(factory, csv_path=csv_path)


@router.post("/basket", response_model=BasketOut)
def basket(
    body: BasketIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
) -> BasketOut:
    recipe_ids = _selection_ids(body.selections)
    _require_curated(session, recipe_ids)
    if not body.selections:
        return _basket_out(Basket())

    index = _load_planner_index(factory, recipe_ids, csv_path)
    selections = [_planner_selection(selection) for selection in body.selections]
    return _basket_out(build_basket(index, selections))


def _candidate_ids(
    session: Session,
    body: SuggestionsIn,
    pinned_ids: set[int],
    factory: sessionmaker[Session],
    csv_path: Path | None,
) -> set[int]:
    """Which recipes the request's filters allow — a membership test, not an order.

    The ranking is computed over the whole library because it does not depend on
    any of this; the filters only decide which of the ranked recipes are shown.
    """
    filters = body.filters.model_dump()
    candidate_ids = set(_filtered_recipe_ids(session, filters)) - pinned_ids
    if "unmapped" not in body.filters.exclude:
        return candidate_ids

    gap_recipe_ids = _recipe_ids_with_pricing_gaps(sorted(candidate_ids), factory, csv_path)
    return candidate_ids - gap_recipe_ids


@router.post("/suggestions", response_model=PlannerSuggestionsOut)
def suggestions(
    body: SuggestionsIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
) -> PlannerSuggestionsOut:
    pinned_recipe_ids = _selection_ids(body.selections)
    _require_curated(session, pinned_recipe_ids)

    pinned = [_planner_selection(selection) for selection in body.selections]
    # Ranked over the whole library and cached against the pinned week, so paging
    # and filter changes are a walk over a list rather than a re-scoring of it.
    ranked = get_ranking(
        factory, pinned, candidate_portions=body.candidate_portions, csv_path=csv_path
    )
    allowed = _candidate_ids(session, body, set(pinned_recipe_ids), factory, csv_path)

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

    personal_ratings = _personal_rating_map(session, page_ids)
    items = []
    for candidate in page_scores:
        recipe = by_id.get(candidate.recipe_id)
        if recipe is None:
            continue
        card = _to_card(recipe, personal_rating=personal_ratings.get(recipe.id)).model_dump()
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
    )
