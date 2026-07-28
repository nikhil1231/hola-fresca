"""Rank the library by what each recipe would add to a week already chosen.

The ranking depends on the pinned week and nothing else. In particular it does
not depend on the browse filters: cuisine, protein and time narrow *which* of the
ranked recipes are shown, never what any of them scores. Keeping that separation
is what lets a filter change or a page turn reuse a ranking that took seconds to
compute.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from app.planner.basket import Selection, score_basket
from app.planner.index import PlanIndex


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


def _need_keys(index: PlanIndex, recipe_id: int) -> set[str]:
    recipe = index.recipes.get(recipe_id)
    return {need.key for need in recipe.needs} if recipe is not None else set()


def rank_candidates(
    index: PlanIndex,
    pinned: Sequence[Selection],
    candidate_ids: Iterable[int],
    *,
    candidate_portions: int,
) -> list[RankedCandidate]:
    """Score every candidate against ``pinned``, cheapest addition first.

    A candidate that shares an ingredient with the week is scored on what it adds
    to the *whole* basket — that is the point of the planner, since the second
    recipe to want coriander gets it for nothing. One that shares nothing can only
    cost what it costs on its own, so it is scored standalone and skips the
    second, more expensive basket entirely.
    """
    candidate_ids = list(candidate_ids)
    base = score_basket(index, pinned) if pinned else None
    base_keys: set[str] = set()
    for selection in pinned:
        base_keys |= _need_keys(index, selection.recipe_id)

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
        standalone = score_basket(index, [candidate])
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
        if base is not None and recipe_id in overlapping:
            combined = score_basket(index, [*pinned, candidate])
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
