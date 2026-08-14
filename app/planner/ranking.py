"""Rank the library by what each recipe would add to a week already chosen.

The ranking depends on the pinned week and nothing else. In particular it does
not depend on the browse filters: cuisine, protein and time narrow *which* of the
ranked recipes are shown, never what any of them scores. Keeping that separation
is what lets a filter change or a page turn reuse a ranking that took seconds to
compute.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol, Sequence

from app.planner.basket import Demand, Selection, cover_need, score_basket
from app.planner.index import PlanIndex, modified_needs


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One recipe's standing against a pinned week."""

    recipe_id: int
    ranking: float
    marginal: float | None
    standalone: float | None
    marginal_cost: float | None
    standalone_cost: float | None
    gap_count: int
    shared: int
    available: bool


class StandalonePrice(Protocol):
    """The cached standalone figures a candidate ranking consumes."""

    score: float
    cost: float
    gap_count: int


def _need_keys(index: PlanIndex, recipe_id: int) -> set[str]:
    recipe = index.recipes.get(recipe_id)
    return {need.key for need in recipe.needs} if recipe is not None else set()


def _selection_demands(
    index: PlanIndex, selections: Sequence[Selection]
) -> dict[str, Demand]:
    """Aggregate just the quantities needed for marginal-price arithmetic."""
    demands: dict[str, Demand] = {}
    for selection in selections:
        recipe = index.recipes.get(selection.recipe_id)
        if recipe is None:
            continue
        servings = selection.servings or recipe.base_yield
        factor = servings / recipe.base_yield if recipe.base_yield else 1.0
        for need in modified_needs(index, recipe, selection.protein):
            grams = need.grams * factor
            units = need.units * factor if need.units is not None else None
            current = demands.get(need.key)
            if current is None:
                demands[need.key] = Demand(grams=grams, units=units)
            else:
                demands[need.key] = Demand(
                    grams=current.grams + grams,
                    units=(current.units or 0.0) + units
                    if units is not None
                    else current.units,
                )
    return demands


def _candidate_shared_demands(
    index: PlanIndex,
    recipe_id: int,
    servings: int,
    shared_keys: set[str],
) -> dict[str, Demand]:
    """Candidate quantities for keys that can change the pinned basket."""
    recipe = index.recipes.get(recipe_id)
    if recipe is None:
        return {}
    factor = servings / recipe.base_yield if recipe.base_yield else 1.0
    demands: dict[str, Demand] = {}
    for need in recipe.needs:
        if need.key not in shared_keys:
            continue
        grams = need.grams * factor
        units = need.units * factor if need.units is not None else None
        current = demands.get(need.key)
        if current is None:
            demands[need.key] = Demand(grams=grams, units=units)
        else:
            demands[need.key] = Demand(
                grams=current.grams + grams,
                units=(current.units or 0.0) + units
                if units is not None
                else current.units,
            )
    return demands


def _cover_totals(
    index: PlanIndex,
    key: str,
    demand: Demand,
    pack_preferences: dict[str, str],
) -> tuple[float, float]:
    """The objective and checkout cost for one independently priced line."""
    ingredient = index.ingredient(key)
    if ingredient is None or ingredient.pantry_staple or not ingredient.shoppable:
        return (0.0, 0.0)
    cover = cover_need(
        index,
        ingredient,
        demand.grams,
        demand.units,
        override=pack_preferences.get(key),
    )
    return (cover.score, cover.cost) if cover is not None else (0.0, 0.0)


def _marginal_from_shared_lines(
    index: PlanIndex,
    recipe_id: int,
    servings: int,
    standalone: StandalonePrice,
    base_demands: dict[str, Demand],
    base_covers: dict[str, tuple[float, float]],
    pack_preferences: dict[str, str],
) -> tuple[float, float]:
    """Adjust a standalone price only where the candidate meets the week.

    Ingredient lines are independent. A candidate-only line therefore adds its
    standalone amount unchanged; for a shared line, replace that standalone
    amount with ``combined - pinned``. This is the same arithmetic as pricing
    the full combined basket, without rebuilding every unaffected line thousands
    of times.
    """
    candidate_demands = _candidate_shared_demands(
        index, recipe_id, servings, set(base_demands)
    )
    marginal_score = standalone.score
    marginal_cost = standalone.cost
    for key, candidate in candidate_demands.items():
        base = base_demands[key]
        candidate_totals = _cover_totals(index, key, candidate, pack_preferences)
        combined = Demand(
            grams=base.grams + candidate.grams,
            units=(base.units or 0.0) + candidate.units
            if candidate.units is not None
            else base.units,
        )
        combined_totals = _cover_totals(index, key, combined, pack_preferences)
        base_totals = base_covers[key]
        marginal_score += combined_totals[0] - base_totals[0] - candidate_totals[0]
        marginal_cost += combined_totals[1] - base_totals[1] - candidate_totals[1]
    return marginal_score, marginal_cost


def rank_candidates(
    index: PlanIndex,
    pinned: Sequence[Selection],
    candidate_ids: Iterable[int],
    *,
    candidate_portions: int,
    pack_preferences: dict[str, str] | None = None,
    standalone_prices: Mapping[int, StandalonePrice] | None = None,
) -> list[RankedCandidate]:
    """Score every candidate against ``pinned``, cheapest addition first.

    A candidate that shares an ingredient with the week is scored on what it adds
    to the *whole* basket — that is the point of the planner, since the second
    recipe to want coriander gets it for nothing. One that shares nothing can only
    cost what it costs on its own, so it is scored standalone and skips the
    second, more expensive basket entirely.

    ``pack_preferences`` are the requesting user's standing pack choices. A
    ranking rarely turns on them, but it has to be scored the same way the basket
    will be priced, or the cheapest-looking recipe would not be the one that came
    out cheapest.
    """
    candidate_ids = list(candidate_ids)
    prefs = pack_preferences or {}
    base = (
        score_basket(index, pinned, pack_preferences=prefs)
        if pinned and standalone_prices is None
        else None
    )
    base_demands = _selection_demands(index, pinned)
    base_keys = set(base_demands)
    base_covers = {
        key: _cover_totals(index, key, demand, prefs)
        for key, demand in base_demands.items()
    }

    recipes_by_key: dict[str, set[int]] = {}
    for recipe_id in candidate_ids:
        for key in _need_keys(index, recipe_id):
            recipes_by_key.setdefault(key, set()).add(recipe_id)
    overlapping: set[int] = set()
    for key in base_keys:
        overlapping |= recipes_by_key.get(key, set())

    ranked: list[RankedCandidate] = []
    for recipe_id in candidate_ids:
        keys = _need_keys(index, recipe_id)
        candidate = Selection(recipe_id=recipe_id, servings=candidate_portions)
        standalone = (
            standalone_prices.get(recipe_id) if standalone_prices is not None else None
        )
        if standalone is None:
            standalone = score_basket(index, [candidate], pack_preferences=prefs)
        available = bool(keys) or standalone.gap_count > 0
        if not available:
            ranked.append(
                RankedCandidate(
                    recipe_id=recipe_id, ranking=float("inf"), marginal=None,
                    standalone=None, marginal_cost=None, standalone_cost=None,
                    gap_count=0, shared=0, available=False,
                )
            )
            continue
        if standalone_prices is not None and recipe_id in overlapping:
            marginal, marginal_cost = _marginal_from_shared_lines(
                index,
                recipe_id,
                candidate_portions,
                standalone,
                base_demands,
                base_covers,
                prefs,
            )
        elif base is not None and recipe_id in overlapping:
            combined = score_basket(index, [*pinned, candidate], pack_preferences=prefs)
            marginal = combined.score - base.score
            marginal_cost = combined.cost - base.cost
        else:
            marginal = standalone.score
            marginal_cost = standalone.cost
        ranked.append(
            RankedCandidate(
                recipe_id=recipe_id,
                ranking=marginal,
                marginal=marginal,
                standalone=standalone.score,
                marginal_cost=marginal_cost,
                standalone_cost=standalone.cost,
                gap_count=standalone.gap_count,
                shared=len(base_keys & keys),
                available=True,
            )
        )

    ranked.sort(key=lambda c: (c.ranking, c.recipe_id))
    return ranked
