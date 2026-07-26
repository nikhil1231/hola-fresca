"""Stateless planner API: basket pricing and best-fit recipe ranking."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.api.deps import get_planner_csv_path, get_session, get_session_factory
from app.api.recipes import _apply_filters, _to_card
from app.api.schemas import (
    BasketIn,
    BasketLineOut,
    BasketOut,
    BasketPackChoiceOut,
    PlannerSuggestionsOut,
    RecipeSuggestionCard,
    SuggestionsIn,
)
from app.db.models import Recipe
from app.planner.basket import Basket, BasketLine, Selection, build_basket
from app.planner.index import PlanIndex, load_index

router = APIRouter(prefix="/api/planner", tags=["planner"])


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
                capacity_g=round(choice.pack.capacity_g, 1),
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
        capacity_g=round(cover.capacity_g, 1) if cover else None,
        leftover_g=round(cover.leftover_g, 1) if cover else None,
        cost=_round_money(line.cost),
        waste_gbp=_round_money(line.waste_gbp),
        score=_round_money(cover.score if cover else 0.0),
        packs=cover.packs if cover else 0,
        trace=line.trace,
        external=line.external,
        note=line.note,
        choices=choices,
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
    return load_index(
        factory,
        recipe_ids=recipe_ids,
        curated_only=False,
        csv_path=csv_path,
    )


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


def _candidate_ids(session: Session, body: SuggestionsIn, pinned_ids: set[int]) -> list[int]:
    filters = body.filters.model_dump()
    stmt = _apply_filters(select(Recipe.id), **filters)
    if pinned_ids:
        stmt = stmt.where(Recipe.id.not_in(pinned_ids))
    return list(session.scalars(stmt).all())


def _recipe_keys(index: PlanIndex, recipe_id: int) -> set[str]:
    recipe = index.recipes.get(recipe_id)
    if recipe is None:
        return set()
    return {need.key for need in recipe.needs}


@router.post("/suggestions", response_model=PlannerSuggestionsOut)
def suggestions(
    body: SuggestionsIn,
    session: Session = Depends(get_session),
    factory: sessionmaker[Session] = Depends(get_session_factory),
    csv_path: Path | None = Depends(get_planner_csv_path),
) -> PlannerSuggestionsOut:
    pinned_recipe_ids = _selection_ids(body.selections)
    _require_curated(session, pinned_recipe_ids)

    pinned_id_set = set(pinned_recipe_ids)
    candidate_ids = _candidate_ids(session, body, pinned_id_set)
    all_index_ids = list(dict.fromkeys([*pinned_recipe_ids, *candidate_ids]))
    index = _load_planner_index(factory, all_index_ids, csv_path)

    pinned = [_planner_selection(selection) for selection in body.selections]
    base = build_basket(index, pinned) if pinned else Basket()
    base_score = base.score
    base_keys: set[str] = set()
    for recipe_id in pinned_recipe_ids:
        base_keys.update(_recipe_keys(index, recipe_id))

    recipes_by_key: dict[str, set[int]] = {}
    for recipe_id in candidate_ids:
        for key in _recipe_keys(index, recipe_id):
            recipes_by_key.setdefault(key, set()).add(recipe_id)
    overlapping_ids = set()
    for key in base_keys:
        overlapping_ids.update(recipes_by_key.get(key, set()))

    scores: list[tuple[float, int, float, int]] = []
    for recipe_id in candidate_ids:
        candidate = Selection(recipe_id=recipe_id, servings=body.candidate_portions)
        standalone = build_basket(index, [candidate]).score
        shared = len(base_keys & _recipe_keys(index, recipe_id))
        if recipe_id in overlapping_ids:
            marginal = build_basket(index, [*pinned, candidate]).score - base_score
        else:
            marginal = standalone
        scores.append((marginal, recipe_id, standalone, shared))

    scores.sort(key=lambda item: (item[0], item[1]))
    total = len(scores)
    start = (body.page - 1) * body.page_size
    page_scores = scores[start:start + body.page_size]
    page_ids = [recipe_id for _, recipe_id, _, _ in page_scores]
    by_id: dict[int, Recipe] = {}
    if page_ids:
        rows = session.scalars(
            select(Recipe)
            .where(Recipe.id.in_(page_ids))
            .options(selectinload(Recipe.cuisines), selectinload(Recipe.tags))
        ).all()
        by_id = {recipe.id: recipe for recipe in rows}

    items = []
    for marginal, recipe_id, standalone, shared in page_scores:
        recipe = by_id.get(recipe_id)
        if recipe is None:
            continue
        card = _to_card(recipe).model_dump()
        items.append(
            RecipeSuggestionCard(
                **card,
                marginal_score=_round_money(marginal),
                standalone_score=_round_money(standalone),
                shared_ingredient_count=shared,
            )
        )

    return PlannerSuggestionsOut(
        items=items,
        total=total,
        page=body.page,
        page_size=body.page_size,
        has_more=start + len(page_scores) < total,
    )
