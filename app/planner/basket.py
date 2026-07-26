"""Turn a set of chosen recipes into a priced basket with a £ waste figure.

This is the planner's inner loop and its objective function in one: sum what the
week actually needs, cover each ingredient from its approved pack sizes as
cheaply as possible, and price the remainder that will not survive to the next
shop. It is pure — everything comes from a :class:`~app.planner.index.PlanIndex`
— so the search can call it thousands of times.

Pack choice is deliberately *not* fixed at mapping time. The mapping settles
which SKUs are acceptable; only here, knowing the week's real demand, is it
possible to say that 1.5 kg of potatoes is better served by one big bag than four
small ones.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable, Sequence

from app.planner import waste as waste_mod
from app.planner.index import Ingredient, Pack, PlanIndex

# Covering resolution. Demand is never accurate to the gram — recipe amounts are
# rounded and packs are sold in round sizes — so the search works in 5 g steps,
# which keeps the state space small at no practical cost in quality.
BUCKET_G = 5
# Refuse to build a DP table larger than this many buckets (~50 kg of a single
# ingredient). Nothing plausible reaches it; it guards against a bad amount_g.
MAX_BUCKETS = 10_000

# Which forms of an ingredient to shop, best first. The reviewer accepts lime
# juice for "Lime" and ginger paste for "Ginger" as *fallbacks*, and per gram they
# are far cheaper than the real thing — so a plain cost objective would buy the
# degraded form every single time. Cover from the best available tier and only
# drop a tier when the one above cannot be bought at all, which keeps the
# reviewer's judgement in charge of identity and leaves the planner in charge of
# pack size.
MATCH_TYPE_PREFERENCE = ("exact", "form_differs", "substitute")

# A demand this small means the recipe wants a trace of something (0.5 ml of
# balsamic, 1 g of mayo) and buying a whole pack for it is an artefact, not a
# shopping decision. Reported separately so it cannot quietly distort a score.
TRACE_NEED_G = 5.0


@dataclass(frozen=True, slots=True)
class PackChoice:
    pack: Pack
    count: int

    @property
    def cost(self) -> float:
        return self.pack.price * self.count

    @property
    def capacity_g(self) -> float:
        return self.pack.capacity_g * self.count


@dataclass(frozen=True, slots=True)
class Cover:
    """How one ingredient's demand is met, and what it costs to meet it."""

    choices: tuple[PackChoice, ...]
    need_g: float
    capacity_g: float
    cost: float
    leftover_g: float
    waste_gbp: float

    @property
    def score(self) -> float:
        """What the planner minimises: cash out of pocket plus cash binned."""
        return self.cost + self.waste_gbp

    @property
    def packs(self) -> int:
        return sum(c.count for c in self.choices)

    def describe(self) -> str:
        return " + ".join(
            f"{c.count}x {c.pack.pack_size_raw or f'{c.pack.capacity_g:g}g'}" for c in self.choices
        )


def _score_multiset(
    choices: Sequence[PackChoice], need_g: float
) -> Cover:
    capacity = sum(c.capacity_g for c in choices)
    cost = sum(c.cost for c in choices)
    leftover = max(0.0, capacity - need_g)
    # Blend salvage across whatever was bought, weighted by how much of the
    # capacity each pack contributes. With a single pack family — the normal case
    # — this is just that pack's salvage fraction.
    salvage = (
        sum(c.capacity_g * c.pack.salvage for c in choices) / capacity if capacity else 0.0
    )
    return Cover(
        choices=tuple(choices),
        need_g=need_g,
        capacity_g=capacity,
        cost=cost,
        leftover_g=leftover,
        waste_gbp=waste_mod.waste_value(leftover, cost, capacity, salvage),
    )


def preferred_packs(ingredient: Ingredient) -> tuple[Pack, ...]:
    """The best-matching form of an ingredient that can actually be bought."""
    for tier in MATCH_TYPE_PREFERENCE:
        tier_packs = tuple(p for p in ingredient.packs if p.match_type == tier)
        if tier_packs:
            return tier_packs
    return ingredient.packs


def _cover_with_packs(packs: Sequence[Pack], need_g: float) -> Cover | None:
    """Cheapest way to cover ``need_g`` from ``packs``, counting waste as cost.

    Exact within the 5 g bucket: an unbounded coin-change over pack sizes finds
    the cheapest multiset for every reachable total capacity, then each total at
    or above the requirement is scored with its own real leftover. That matters
    because the cheapest cover and the least wasteful one are often different
    packs, and the tie is broken in money.
    """
    target_b = math.ceil(need_g / BUCKET_G)
    if target_b > MAX_BUCKETS:
        return None

    caps_b = [max(1, round(p.capacity_g / BUCKET_G)) for p in packs]
    limit = min(target_b + max(caps_b), MAX_BUCKETS)

    inf = float("inf")
    dp = [inf] * (limit + 1)
    last = [-1] * (limit + 1)
    dp[0] = 0.0
    for s in range(1, limit + 1):
        best = inf
        best_i = -1
        for i, cap_b in enumerate(caps_b):
            if cap_b > s:
                continue
            prev = dp[s - cap_b]
            if prev == inf:
                continue
            candidate = prev + packs[i].price
            if candidate < best:
                best = candidate
                best_i = i
        dp[s] = best
        last[s] = best_i

    best_cover: Cover | None = None
    for s in range(target_b, limit + 1):
        if dp[s] == inf:
            continue
        counts: dict[int, int] = defaultdict(int)
        cursor = s
        while cursor > 0:
            i = last[cursor]
            if i < 0:
                break
            counts[i] += 1
            cursor -= caps_b[i]
        if not counts:
            continue
        choices = [PackChoice(pack=packs[i], count=n) for i, n in sorted(counts.items())]
        candidate = _score_multiset(choices, need_g)
        if best_cover is None or candidate.score < best_cover.score:
            best_cover = candidate

    return best_cover


def cover_need(index: PlanIndex, ingredient: Ingredient, need_g: float) -> Cover | None:
    """Cover ``need_g`` of ``ingredient``, memoised on the index.

    Demand is bucketed for the cache too, so the thousands of near-identical
    queries a search makes collapse onto a handful of real computations.
    """
    if need_g <= 0 or not ingredient.packs:
        return None
    target_b = math.ceil(need_g / BUCKET_G)
    cache_key = (ingredient.key, target_b)
    if cache_key in index.cover_cache:
        return index.cover_cache[cache_key]  # type: ignore[return-value]
    cover = _cover_with_packs(preferred_packs(ingredient), need_g)
    index.cover_cache[cache_key] = cover
    return cover


# --------------------------------------------------------------------------
# Basket assembly
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Selection:
    """A recipe in the week's plan, cooked for ``servings`` people."""

    recipe_id: int
    servings: int | None = None


@dataclass(frozen=True, slots=True)
class BasketContribution:
    recipe_id: int
    recipe_name: str
    grams: float


@dataclass
class BasketLine:
    key: str
    name: str
    need_g: float
    cover: Cover | None = None
    note: str | None = None
    contributions: tuple[BasketContribution, ...] = ()

    @property
    def cost(self) -> float:
        return self.cover.cost if self.cover else 0.0

    @property
    def waste_gbp(self) -> float:
        return self.cover.waste_gbp if self.cover else 0.0

    @property
    def trace(self) -> bool:
        """A whole pack bought to satisfy a trace demand — see ``TRACE_NEED_G``."""
        return self.need_g <= TRACE_NEED_G

    @property
    def external(self) -> bool:
        """Bought somewhere other than the shop being planned for."""
        return bool(self.cover and self.cover.choices[0].pack.external)


@dataclass
class Basket:
    lines: list[BasketLine] = field(default_factory=list)
    # Mapped, approved, but assumed already in the cupboard.
    staples: list[str] = field(default_factory=list)
    # Demanded by a recipe with no approved mapping to shop from.
    unmapped: list[str] = field(default_factory=list)
    # Mapped, but no accepted product yields a usable pack size or price.
    unpriceable: list[str] = field(default_factory=list)
    untracked_lines: int = 0

    @property
    def cost(self) -> float:
        return sum(line.cost for line in self.lines)

    @property
    def waste_gbp(self) -> float:
        return sum(line.waste_gbp for line in self.lines)

    @property
    def score(self) -> float:
        """The planner's objective for this basket: spend plus waste."""
        return self.cost + self.waste_gbp

    @property
    def priced_lines(self) -> list[BasketLine]:
        return [line for line in self.lines if line.cover is not None]

    @property
    def trace_lines(self) -> list[BasketLine]:
        """Lines bought for a trace demand: candidates for a pantry-staple flag."""
        return [line for line in self.lines if line.trace and line.cover is not None]

    @property
    def retailer_lines(self) -> list[BasketLine]:
        """The actual online order."""
        return [line for line in self.lines if not line.external]

    @property
    def external_lines(self) -> list[BasketLine]:
        """Sourced by hand elsewhere — still costed, just not in the order."""
        return [line for line in self.lines if line.external]


def aggregate_needs(
    index: PlanIndex, selections: Iterable[Selection]
) -> tuple[
    dict[str, float],
    dict[str, str],
    int,
    dict[str, dict[int, BasketContribution]],
]:
    """Total grams needed per canonical ingredient across the week.

    Recipe amounts are stored at the recipe's base yield, so cooking for more
    people scales them linearly. Demand for the same ingredient from different
    recipes sums into one pack decision — which is the whole point.
    """
    needs: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}
    contributions: dict[str, dict[int, BasketContribution]] = defaultdict(dict)
    untracked = 0
    for selection in selections:
        recipe = index.recipes.get(selection.recipe_id)
        if recipe is None:
            continue
        servings = selection.servings or recipe.base_yield
        factor = servings / recipe.base_yield if recipe.base_yield else 1.0
        untracked += recipe.untracked_lines
        for need in recipe.needs:
            grams = need.grams * factor
            needs[need.key] += grams
            names.setdefault(need.key, need.display_name)
            existing = contributions[need.key].get(recipe.id)
            contributions[need.key][recipe.id] = BasketContribution(
                recipe_id=recipe.id,
                recipe_name=recipe.name,
                grams=(existing.grams if existing else 0.0) + grams,
            )
    return needs, names, untracked, contributions


def build_basket(
    index: PlanIndex,
    selections: Iterable[Selection],
    *,
    include_staples: bool = False,
) -> Basket:
    """Price a week's recipes: one pack decision per canonical ingredient."""
    needs, names, untracked, contributions = aggregate_needs(index, selections)
    basket = Basket(untracked_lines=untracked)

    for key, grams in needs.items():
        label = names.get(key, key)
        ingredient = index.ingredient(key)
        if ingredient is None:
            basket.unmapped.append(label)
            continue
        if ingredient.pantry_staple and not include_staples:
            basket.staples.append(label)
            continue
        if not ingredient.shoppable:
            basket.unpriceable.append(label)
            continue
        cover = cover_need(index, ingredient, grams)
        line = BasketLine(
            key=key,
            name=label,
            need_g=round(grams, 1),
            cover=cover,
            contributions=tuple(
                BasketContribution(
                    recipe_id=c.recipe_id,
                    recipe_name=c.recipe_name,
                    grams=round(c.grams, 1),
                )
                for c in sorted(contributions.get(key, {}).values(), key=lambda c: c.recipe_id)
            ),
        )
        if cover is None:
            line.note = "no pack covers this demand"
        basket.lines.append(line)

    basket.lines.sort(key=lambda line: line.cost, reverse=True)
    basket.staples.sort()
    basket.unmapped.sort()
    basket.unpriceable.sort()
    return basket


def basket_gap_count(basket: Basket) -> int:
    """Unknown demand lines that should not rank as free."""
    return len(basket.unmapped) + len(basket.unpriceable) + basket.untracked_lines


def median_priced_line_cost(index: PlanIndex) -> float:
    costs: list[float] = []
    for recipe in index.recipes.values():
        for need in recipe.needs:
            ingredient = index.ingredient(need.key)
            if ingredient is None or ingredient.pantry_staple or not ingredient.shoppable:
                continue
            cover = cover_need(index, ingredient, need.grams)
            if cover is not None and cover.cost > 0:
                costs.append(cover.cost)
    return float(median(costs)) if costs else 0.0
