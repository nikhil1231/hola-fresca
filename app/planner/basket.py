"""Turn a set of chosen recipes into a priced basket with a GBP waste figure.

This is the planner's inner loop and its objective function in one: sum what the
week actually needs, cover each ingredient from its approved pack sizes as
cheaply as possible, and price the remainder that will not survive to the next
shop. It is pure; everything comes from a :class:`~app.planner.index.PlanIndex`,
so the search can call it thousands of times.

Pack choice is deliberately *not* fixed at mapping time. The mapping settles
which SKUs are acceptable; only here, knowing the week's real demand, is it
possible to say that 1.5 kg of potatoes is better served by one big bag than four
small ones.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from statistics import median
from typing import Iterable, Sequence

from app.planner import waste as waste_mod
from app.planner.index import Ingredient, Pack, PlanIndex

BUCKET_G = 5
MAX_BUCKETS = 10_000
MATCH_TYPE_PREFERENCE = ("exact", "form_differs", "substitute")
TRACE_NEED_G = 5.0
COUNT_CEIL_EPSILON = 1e-6


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

    @property
    def capacity_qty(self) -> float:
        return self.pack.capacity_qty * self.count


@dataclass(frozen=True, slots=True)
class Substitution:
    """What a cover would have bought if the shop had everything in stock.

    Recorded rather than inferred, because the swap is invisible after the fact:
    once the sold-out pack is filtered out, the cover that replaces it looks like
    a first choice. The price delta is the honest cost of the substitution — what
    this ingredient costs now, minus what it would have cost.
    """

    displaced: tuple[str, ...]
    displaced_skus: tuple[str, ...]
    baseline_cost: float
    cost_delta: float
    #: The best-matching form ran out entirely, so the cover dropped a tier -
    #: "roasted white sesame" giving way to plain sesame seeds. Worth saying out
    #: loud: it is a change of ingredient, not just of brand.
    tier_changed: bool = False


@dataclass(frozen=True, slots=True)
class Cover:
    """How one ingredient's demand is met, and what it costs to meet it."""

    choices: tuple[PackChoice, ...]
    need_g: float
    capacity_g: float
    cost: float
    leftover_g: float
    waste_gbp: float
    quantity_unit: str = "g"
    need_qty: float | None = None
    capacity_qty: float | None = None
    leftover_qty: float | None = None
    substitution: Substitution | None = None

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
    choices: Sequence[PackChoice], need_g: float, *, need_qty: float | None = None,
    quantity_unit: str = "g",
) -> Cover:
    capacity_g = sum(c.capacity_g for c in choices)
    cost = sum(c.cost for c in choices)
    capacity_qty = sum(c.capacity_qty for c in choices) if need_qty is not None else None
    leftover_qty = max(0.0, capacity_qty - need_qty) if capacity_qty is not None else None
    leftover_g = max(0.0, capacity_g - need_g)
    salvage_basis_capacity = capacity_qty if capacity_qty is not None else capacity_g
    salvage_basis_leftover = leftover_qty if leftover_qty is not None else leftover_g
    salvage = (
        sum(c.capacity_qty * c.pack.salvage for c in choices) / salvage_basis_capacity
        if capacity_qty is not None and salvage_basis_capacity
        else sum(c.capacity_g * c.pack.salvage for c in choices) / capacity_g
        if capacity_g
        else 0.0
    )
    return Cover(
        choices=tuple(choices),
        need_g=need_g,
        capacity_g=capacity_g,
        cost=cost,
        leftover_g=leftover_g,
        waste_gbp=waste_mod.waste_value(
            salvage_basis_leftover, cost, salvage_basis_capacity, salvage
        ),
        quantity_unit=quantity_unit,
        need_qty=need_qty,
        capacity_qty=capacity_qty,
        leftover_qty=leftover_qty,
    )


def preferred_packs(
    ingredient: Ingredient, *, include_unavailable: bool = False
) -> tuple[Pack, ...]:
    """The best-matching form of an ingredient that can actually be bought.

    "Can actually be bought" now means in stock as well as approved, and the
    walk down the tiers is what makes that survivable: when every exact match is
    sold out the cover drops to ``form_differs`` rather than giving up on the
    ingredient. ``include_unavailable`` asks the same question of a shop with
    full shelves, which is how the substitution's price delta is measured.
    """
    return _preferred_tier(ingredient, include_unavailable=include_unavailable)[1]


def _preferred_tier(
    ingredient: Ingredient, *, include_unavailable: bool = False
) -> tuple[str | None, tuple[Pack, ...]]:
    packs = ingredient.packs if include_unavailable else ingredient.available_packs
    for tier in MATCH_TYPE_PREFERENCE:
        tier_packs = tuple(p for p in packs if p.match_type == tier)
        if tier_packs:
            return tier, tier_packs
    return (None, packs)


def _cover_with_packs(packs: Sequence[Pack], need_g: float) -> Cover | None:
    """Cheapest way to cover ``need_g`` from ``packs``, counting waste as cost."""
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


def _cover_count_with_packs(packs: Sequence[Pack], need_units: float, need_g: float) -> Cover | None:
    target = math.ceil(need_units - COUNT_CEIL_EPSILON)
    if target <= 0:
        return None
    caps = [max(1, int(round(p.capacity_qty))) for p in packs]
    limit = target + max(caps)

    inf = float("inf")
    dp = [inf] * (limit + 1)
    last = [-1] * (limit + 1)
    dp[0] = 0.0
    for s in range(1, limit + 1):
        best = inf
        best_i = -1
        for i, cap in enumerate(caps):
            if cap > s:
                continue
            prev = dp[s - cap]
            if prev == inf:
                continue
            candidate = prev + packs[i].price
            if candidate < best:
                best = candidate
                best_i = i
        dp[s] = best
        last[s] = best_i

    best_cover: Cover | None = None
    for s in range(target, limit + 1):
        if dp[s] == inf:
            continue
        counts: dict[int, int] = defaultdict(int)
        cursor = s
        while cursor > 0:
            i = last[cursor]
            if i < 0:
                break
            counts[i] += 1
            cursor -= caps[i]
        if not counts:
            continue
        choices = [PackChoice(pack=packs[i], count=n) for i, n in sorted(counts.items())]
        candidate = _score_multiset(
            choices,
            need_g,
            need_qty=need_units,
            quantity_unit="unit",
        )
        if best_cover is None or candidate.score < best_cover.score:
            best_cover = candidate

    return best_cover


def _cover_from(
    packs: Sequence[Pack],
    unit_kind: str,
    need_g: float,
    need_units: float | None,
) -> Cover | None:
    if not packs:
        return None
    if unit_kind == "count":
        return _cover_count_with_packs(packs, need_units or 0.0, need_g)
    return _cover_with_packs(packs, need_g)


def _with_substitution(
    cover: Cover | None,
    ingredient: Ingredient,
    need_g: float,
    need_units: float | None,
) -> Cover | None:
    """Label a cover with what being sold out cost it, if anything.

    Only asked of ingredients that actually have a sold-out pack, and only then
    is the second, hypothetical cover computed - so the common case pays nothing
    for this.
    """
    if cover is None:
        return None
    tier, _ = _preferred_tier(ingredient)
    full_tier, full_packs = _preferred_tier(ingredient, include_unavailable=True)
    baseline = _cover_from(full_packs, ingredient.unit_kind, need_g, need_units)
    if baseline is None:
        return cover

    displaced = tuple(choice.pack for choice in baseline.choices if not choice.pack.available)
    if not displaced:
        # Everything sold out was something this ingredient would not have
        # bought anyway - the cheapest packs survived, so nothing was displaced.
        return cover
    return replace(
        cover,
        substitution=Substitution(
            displaced=tuple(pack.product_name for pack in displaced),
            displaced_skus=tuple(pack.sku for pack in displaced),
            baseline_cost=baseline.cost,
            cost_delta=cover.cost - baseline.cost,
            tier_changed=tier != full_tier,
        ),
    )


def cover_need(
    index: PlanIndex, ingredient: Ingredient, need_g: float, need_units: float | None = None
) -> Cover | None:
    """Cover one ingredient's demand, memoised on the index."""
    if not ingredient.available_packs:
        return None
    if ingredient.unit_kind == "count":
        if need_units is None or need_units <= 0:
            return None
        target = math.ceil(need_units - COUNT_CEIL_EPSILON)
        cache_key = (ingredient.key, "count", target)
        if cache_key in index.cover_cache:
            return index.cover_cache[cache_key]  # type: ignore[return-value]
        cover = _cover_count_with_packs(preferred_packs(ingredient), need_units, need_g)
    elif need_g <= 0:
        return None
    else:
        target_b = math.ceil(need_g / BUCKET_G)
        cache_key = (ingredient.key, "g", target_b)
        if cache_key in index.cover_cache:
            return index.cover_cache[cache_key]  # type: ignore[return-value]
        cover = _cover_with_packs(preferred_packs(ingredient), need_g)

    if len(ingredient.available_packs) != len(ingredient.packs):
        cover = _with_substitution(cover, ingredient, need_g, need_units)
    index.cover_cache[cache_key] = cover
    return cover


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
    quantity: float | None = None
    quantity_unit: str = "g"


@dataclass(frozen=True, slots=True)
class Demand:
    grams: float = 0.0
    units: float | None = None


@dataclass
class BasketLine:
    key: str
    name: str
    need_g: float
    cover: Cover | None = None
    note: str | None = None
    contributions: tuple[BasketContribution, ...] = ()
    unit_kind: str = "mass"
    need_qty: float | None = None
    quantity_unit: str = "g"

    @property
    def cost(self) -> float:
        return self.cover.cost if self.cover else 0.0

    @property
    def waste_gbp(self) -> float:
        return self.cover.waste_gbp if self.cover else 0.0

    @property
    def substitution(self) -> Substitution | None:
        """Set when something this line wanted was sold out - see :class:`Substitution`."""
        return self.cover.substitution if self.cover else None

    @property
    def trace(self) -> bool:
        """A whole pack bought to satisfy a trace demand - see ``TRACE_NEED_G``."""
        return self.unit_kind != "count" and self.need_g <= TRACE_NEED_G

    @property
    def external(self) -> bool:
        """Bought somewhere other than the shop being planned for."""
        return bool(self.cover and self.cover.choices[0].pack.external)

    @property
    def consumed_cost(self) -> float:
        if self.cover is None or self.cost <= 0:
            return 0.0
        if self.unit_kind == "count" and self.need_qty is not None and self.cover.capacity_qty:
            return self.cost * (self.need_qty / self.cover.capacity_qty)
        if self.cover.capacity_g:
            return self.cost * (self.need_g / self.cover.capacity_g)
        return 0.0


@dataclass
class Basket:
    lines: list[BasketLine] = field(default_factory=list)
    staples: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    unpriceable: list[str] = field(default_factory=list)
    #: Mapped and priced, but every approved product is out of stock today. Held
    #: apart from ``unpriceable`` because it is a fact about this morning rather
    #: than about the mapping, and it will fix itself.
    sold_out: list[str] = field(default_factory=list)
    untracked_lines: int = 0

    @property
    def cost(self) -> float:
        return sum(line.cost for line in self.lines)

    @property
    def consumed_cost(self) -> float:
        return sum(line.consumed_cost for line in self.lines)

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
    def substituted_lines(self) -> list[BasketLine]:
        """Lines covering around something the shop has run out of."""
        return [line for line in self.lines if line.substitution is not None]

    @property
    def retailer_lines(self) -> list[BasketLine]:
        """The actual online order."""
        return [line for line in self.lines if not line.external]

    @property
    def external_lines(self) -> list[BasketLine]:
        """Sourced by hand elsewhere - still costed, just not in the order."""
        return [line for line in self.lines if line.external]


def aggregate_needs(
    index: PlanIndex, selections: Iterable[Selection]
) -> tuple[
    dict[str, Demand],
    dict[str, str],
    int,
    dict[str, dict[int, BasketContribution]],
]:
    """Total demand per canonical ingredient across the week."""
    needs: dict[str, Demand] = defaultdict(Demand)
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
            units = need.units * factor if need.units is not None else None
            current = needs[need.key]
            needs[need.key] = Demand(
                grams=current.grams + grams,
                units=(current.units or 0.0) + units if units is not None else current.units,
            )
            names.setdefault(need.key, need.display_name)
            existing = contributions[need.key].get(recipe.id)
            contributions[need.key][recipe.id] = BasketContribution(
                recipe_id=recipe.id,
                recipe_name=recipe.name,
                grams=(existing.grams if existing else 0.0) + grams,
                quantity=((existing.quantity or 0.0) if existing else 0.0) + units
                if units is not None
                else None,
                quantity_unit="unit" if units is not None else "g",
            )
    return needs, names, untracked, contributions


@dataclass(frozen=True, slots=True)
class BasketScore:
    """Only the figures a ranking compares — no line detail, no contributions."""

    score: float
    cost: float
    consumed_cost: float
    gap_count: int


def score_basket(index: PlanIndex, selections: Iterable[Selection]) -> BasketScore:
    """Price a selection without building the itemised basket.

    :func:`build_basket` spends most of its time assembling things a ranking
    never reads — per-line contribution tuples, rounded display quantities, the
    cost-ordered line list. Ranking the library calls this once per candidate and
    then some, so it walks the same decisions and keeps only the totals. It must
    agree with ``build_basket`` exactly; ``test_score_basket_agrees_with_build_basket``
    holds the two together.
    """
    needs: dict[str, Demand] = {}
    gaps = 0
    for selection in selections:
        recipe = index.recipes.get(selection.recipe_id)
        if recipe is None:
            continue
        servings = selection.servings or recipe.base_yield
        factor = servings / recipe.base_yield if recipe.base_yield else 1.0
        gaps += recipe.untracked_lines
        for need in recipe.needs:
            grams = need.grams * factor
            units = need.units * factor if need.units is not None else None
            current = needs.get(need.key)
            if current is None:
                needs[need.key] = Demand(grams=grams, units=units)
            else:
                needs[need.key] = Demand(
                    grams=current.grams + grams,
                    units=(current.units or 0.0) + units if units is not None else current.units,
                )

    cost = 0.0
    waste = 0.0
    consumed = 0.0
    for key, demand in needs.items():
        ingredient = index.ingredient(key)
        if ingredient is None:
            gaps += 1
            continue
        if ingredient.pantry_staple:
            continue
        if not ingredient.shoppable:
            gaps += 1
            continue
        cover = cover_need(index, ingredient, demand.grams, demand.units)
        if cover is None:
            continue
        cost += cover.cost
        waste += cover.waste_gbp
        # Mirrors ``BasketLine.consumed_cost``, rounding the demand exactly as the
        # line would, so the pro-rata figure on a card is the same either way.
        if cover.cost <= 0:
            continue
        if ingredient.unit_kind == "count" and demand.units is not None and cover.capacity_qty:
            consumed += cover.cost * (round(demand.units, 3) / cover.capacity_qty)
        elif cover.capacity_g:
            consumed += cover.cost * (round(demand.grams, 1) / cover.capacity_g)
    return BasketScore(
        score=cost + waste, cost=cost, consumed_cost=consumed, gap_count=gaps
    )


def build_basket(
    index: PlanIndex,
    selections: Iterable[Selection],
    *,
    include_staples: bool = False,
) -> Basket:
    """Price a week's recipes: one pack decision per canonical ingredient."""
    needs, names, untracked, contributions = aggregate_needs(index, selections)
    basket = Basket(untracked_lines=untracked)

    for key, demand in needs.items():
        label = names.get(key, key)
        ingredient = index.ingredient(key)
        if ingredient is None:
            basket.unmapped.append(label)
            continue
        if ingredient.pantry_staple and not include_staples:
            basket.staples.append(label)
            continue
        if not ingredient.shoppable:
            if ingredient.sold_out:
                basket.sold_out.append(label)
            else:
                basket.unpriceable.append(label)
            continue
        cover = cover_need(index, ingredient, demand.grams, demand.units)
        line = BasketLine(
            key=key,
            name=label,
            need_g=round(demand.grams, 1),
            cover=cover,
            unit_kind=ingredient.unit_kind,
            need_qty=round(demand.units, 3) if demand.units is not None else None,
            quantity_unit="unit" if ingredient.unit_kind == "count" else "g",
            contributions=tuple(
                BasketContribution(
                    recipe_id=c.recipe_id,
                    recipe_name=c.recipe_name,
                    grams=round(c.grams, 1),
                    quantity=round(c.quantity, 3) if c.quantity is not None else None,
                    quantity_unit=c.quantity_unit,
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
    basket.sold_out.sort()
    return basket


def basket_gap_count(basket: Basket) -> int:
    """Unknown demand lines that should not rank as free."""
    return (
        len(basket.unmapped)
        + len(basket.unpriceable)
        + len(basket.sold_out)
        + basket.untracked_lines
    )
