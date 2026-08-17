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
from app.planner.index import Ingredient, Pack, PlanIndex, modified_needs
from app.protein import ProteinModifier

BUCKET_G = 5
MAX_BUCKETS = 10_000
MATCH_TYPE_PREFERENCE = ("exact", "form_differs", "substitute")
TRACE_NEED_G = 5.0
COUNT_CEIL_EPSILON = 1e-6

# Quality. Ratings cover the whole approved catalogue (median 4.2 stars, a fifth
# of it below 3.5, and a median spread of a full star between the products
# approved for the same ingredient), so "cheapest" and "best" genuinely come
# apart. Measured over 120 simulated weeks, this floor moves 3% of lines and
# costs 0.6% more for a median gain of half a star; raising it to 3.5 costs 4%.
RATING_MIN_COUNT = 5
RATING_FLOOR = 3.0
RATING_MAX_DROP = 1.0


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
    #: Capacity-weighted salvage of the packs chosen. Already priced into
    #: ``waste_gbp``; carried separately because the pantry needs it raw — a lot
    #: deposited from this cover decays on the salvage of what was bought.
    salvage: float = 0.0

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
        salvage=salvage,
    )


def preferred_packs(
    ingredient: Ingredient, *, include_unavailable: bool = False, override: str | None = None
) -> tuple[Pack, ...]:
    """The best-matching form of an ingredient that can actually be bought.

    "Can actually be bought" now means in stock as well as approved, and the
    walk down the tiers is what makes that survivable: when every exact match is
    sold out the cover drops to ``form_differs`` rather than giving up on the
    ingredient. ``include_unavailable`` asks the same question of a shop with
    full shelves, which is how the substitution's price delta is measured.

    A chosen pack beats all of it - having decided you buy the kilo bag of rice,
    whether just this week or always, the planner should stop re-deciding it -
    but only while that product is in stock, and never for the hypothetical
    baseline, which has to describe the shop rather than your preferences.
    ``override`` is that choice, already resolved by :func:`chosen_sku`.
    """
    if not include_unavailable:
        pinned = pinned_pack(ingredient, override)
        if pinned is not None:
            return (pinned,)
    return _preferred_tier(ingredient, include_unavailable=include_unavailable)[1]


def pinned_pack(ingredient: Ingredient, sku: str | None = None) -> Pack | None:
    """The pack this ingredient is bought as, if it can be bought today.

    ``sku`` is the chosen pack — this week's choice if there is one, otherwise
    the standing preference. The two are resolved into one before they get here
    (see :func:`chosen_sku`), because from the covering's point of view they are
    the same instruction; the difference matters only when the choice is shown
    back to the user.
    """
    if not sku:
        return None
    return next((p for p in ingredient.available_packs if p.sku == sku), None)


def chosen_sku(week_override: str | None, standing: str | None) -> str | None:
    """Which pack choice wins for one ingredient.

    This week's beats the standing one: deciding to buy the big bag once is a
    different decision from always buying it, and the first should not quietly
    become the second.
    """
    return week_override or standing


def _preferred_tier(
    ingredient: Ingredient, *, include_unavailable: bool = False
) -> tuple[str | None, tuple[Pack, ...]]:
    packs = ingredient.packs if include_unavailable else ingredient.available_packs
    for tier in MATCH_TYPE_PREFERENCE:
        tier_packs = tuple(p for p in packs if p.match_type == tier)
        if tier_packs:
            return tier, drop_poorly_rated(tier_packs)
    return (None, drop_poorly_rated(packs))


def credibly_rated(pack: Pack) -> bool:
    """Enough ratings to be evidence rather than an anecdote."""
    return pack.rating is not None and (pack.ratings_count or 0) >= RATING_MIN_COUNT


def drop_poorly_rated(packs: tuple[Pack, ...]) -> tuple[Pack, ...]:
    """Take the bad products off the shortlist, but only when there is a choice.

    Cheapest-per-kilo is a bad objective on its own: the cheapest garlic in the
    mapping is a 2.2-star product sitting beside a 3.8-star one for less money.
    A pack is only dropped when its rating is both poor outright and clearly
    beaten, and never when dropping it would leave nothing to buy - an
    unpopular ingredient is still an ingredient the recipe asked for.
    """
    best = max((p.rating for p in packs if credibly_rated(p)), default=None)
    if best is None:
        return packs
    keep = tuple(p for p in packs if not _poorly_rated(p, best))
    return keep or packs


def _poorly_rated(pack: Pack, best: float) -> bool:
    return (
        credibly_rated(pack)
        and pack.rating < RATING_FLOOR
        and best - pack.rating > RATING_MAX_DROP
    )


def _rating_downgrade(option: Pack, current: Pack) -> bool:
    """Whether swapping ``current`` for ``option`` is a step down in quality.

    Five stars to three is not always worth the saving, so a deal has to clear
    this before it is offered - the money is only half of the comparison.
    """
    if not credibly_rated(option):
        return False
    if option.rating < RATING_FLOOR:
        return True
    if not credibly_rated(current):
        return False
    return current.rating - option.rating > RATING_MAX_DROP


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
    best_s = -1
    for s in range(target_b, limit + 1):
        if dp[s] == inf:
            continue
        choices = _rebuild(dp, last, caps_b, packs, s)
        if not choices:
            continue
        candidate = _score_multiset(choices, need_g)
        if best_cover is None or candidate.score < best_cover.score:
            best_cover = candidate
            best_s = s

    upgrade = _better_deal(dp, last, caps_b, packs, best_s, limit, best_cover)
    return _score_multiset(upgrade, need_g) if upgrade else best_cover


def _rebuild(
    dp: list[float],
    last: list[int],
    caps: list[int],
    packs: Sequence[Pack],
    s: int,
) -> list[PackChoice]:
    counts: dict[int, int] = defaultdict(int)
    cursor = s
    while cursor > 0:
        i = last[cursor]
        if i < 0:
            break
        counts[i] += 1
        cursor -= caps[i]
    return [PackChoice(pack=packs[i], count=n) for i, n in sorted(counts.items())]


def _better_deal(
    dp: list[float],
    last: list[int],
    caps: list[int],
    packs: Sequence[Pack],
    best_s: int,
    limit: int,
    best_cover: Cover | None,
) -> list[PackChoice] | None:
    """Swap the scored choice for one that is more food *and* strictly less money.

    The score minimises spend plus waste, and waste can talk it out of a genuine
    price cut - it was buying the smaller, dearer bag of tomatoes because the
    bigger cheaper one left more over. Paying more for less is not a trade-off
    worth modelling, whatever becomes of the remainder.

    Applied to the decision rather than to the candidate list, which matters: an
    earlier version struck the beaten options out before scoring, and the scoring
    then settled on something dearer than the option it had just rejected.
    """
    if best_cover is None or best_s < 0:
        return None
    cheapest = min(
        (s for s in range(best_s, limit + 1) if dp[s] < best_cover.cost - 1e-9),
        key=lambda s: dp[s],
        default=None,
    )
    if cheapest is None:
        return None
    return _rebuild(dp, last, caps, packs, cheapest) or None


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

    def score(choices: list[PackChoice]) -> Cover:
        return _score_multiset(choices, need_g, need_qty=need_units, quantity_unit="unit")

    best_cover: Cover | None = None
    best_s = -1
    for s in range(target, limit + 1):
        if dp[s] == inf:
            continue
        choices = _rebuild(dp, last, caps, packs, s)
        if not choices:
            continue
        candidate = score(choices)
        if best_cover is None or candidate.score < best_cover.score:
            best_cover = candidate
            best_s = s

    upgrade = _better_deal(dp, last, caps, packs, best_s, limit, best_cover)
    return score(upgrade) if upgrade else best_cover


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
    index: PlanIndex,
    ingredient: Ingredient,
    need_g: float,
    need_units: float | None = None,
    *,
    override: str | None = None,
) -> Cover | None:
    """Cover one ingredient's demand, memoised on the index.

    ``override`` is the pack that was chosen — this week's, or the user's
    standing preference — so it joins the cache key rather than replacing the
    entry a plain week would use. That is load-bearing now the index is shared
    between users: without the sku in the key, one person's standing choice would
    be served to the next person to ask about the same ingredient.
    """
    if not ingredient.available_packs:
        return None
    packs = preferred_packs(ingredient, override=override)
    if ingredient.unit_kind == "count":
        if need_units is None or need_units <= 0:
            return None
        target = math.ceil(need_units - COUNT_CEIL_EPSILON)
        cache_key = (ingredient.key, "count", target, override)
        if cache_key in index.cover_cache:
            return index.cover_cache[cache_key]  # type: ignore[return-value]
        cover = _cover_count_with_packs(packs, need_units, need_g)
    elif need_g <= 0:
        return None
    else:
        target_b = math.ceil(need_g / BUCKET_G)
        cache_key = (ingredient.key, "g", target_b, override)
        if cache_key in index.cover_cache:
            return index.cover_cache[cache_key]  # type: ignore[return-value]
        cover = _cover_with_packs(packs, need_g)

    if len(ingredient.available_packs) != len(ingredient.packs):
        cover = _with_substitution(cover, ingredient, need_g, need_units)
    index.cover_cache[cache_key] = cover
    return cover


# What it takes for a bigger pack to be worth recommending rather than merely
# listing. The planner prices one week, so on its own arithmetic the small pack
# always wins - the cheaper £/kg only pays off in weeks it cannot see. These are
# the conditions under which those weeks are a safe enough bet.
BULK_MIN_UNIT_SAVING = 0.15  # a real difference in £/kg, not rounding
BULK_MIN_SALVAGE = 0.65      # the remainder has to survive to be used
BULK_MIN_RECIPE_PCT = 1.0    # and the ingredient has to come back: the top ~12%
BULK_MAX_EXTRA_GBP = 3.00    # thrift you have to fund is not thrift
DEFAULT_PACK_SHORTFALL_TOLERANCE_PCT = 10.0


@dataclass(frozen=True, slots=True)
class PackOption:
    """What buying this ingredient in one particular size would cost this week.

    One entry per approved pack of the best-matching form, each covered on its
    own, so the choice can be shown as the trade-off it is: £/kg against cash
    now and a cupboard full of the remainder.
    """

    pack: Pack
    count: int
    cost: float
    capacity: float
    leftover: float
    unit_cost: float
    cost_delta: float
    leftover_delta: float
    quantity_unit: str = "g"
    chosen: bool = False
    #: Standing choice, held on the mapping and honoured every week.
    pinned: bool = False
    #: Chosen for this week only, and gone by the next one.
    this_week: bool = False
    better_value: bool = False
    recommended: bool = False
    recommendation_reason: str | None = None
    shortfall: float = 0.0
    shortfall_pct: float = 0.0
    #: Roughly how long this pack lasts, allowing for how often the library
    #: actually cooks the ingredient. The one thing the gates cannot judge -
    #: whether *you* will get through it - so it is shown rather than scored.
    supply: Supply | None = None

    @property
    def weeks_of_supply(self) -> float | None:
        return self.supply.weeks if self.supply else None

    @property
    def keeps(self) -> bool:
        return self.pack.salvage >= BULK_MIN_SALVAGE

    @property
    def form_differs(self) -> bool:
        return self.pack.match_type == "form_differs"


def _pack_option(
    pack: Pack, need: float, unit: str, *, shortfall_tolerance_pct: float = 0.0
) -> PackOption | None:
    capacity_each = pack.capacity_qty if unit == "unit" else pack.capacity_g
    if not capacity_each or capacity_each <= 0 or need <= 0:
        return None
    count = max(1, math.ceil(need / capacity_each - COUNT_CEIL_EPSILON))
    if count > 1 and shortfall_tolerance_pct > 0:
        lower_count = count - 1
        lower_capacity = capacity_each * lower_count
        shortfall_pct = 100 * max(0.0, need - lower_capacity) / need
        if 0 < shortfall_pct <= shortfall_tolerance_pct:
            count = lower_count
    capacity = capacity_each * count
    shortfall = max(0.0, need - capacity)
    return PackOption(
        pack=pack,
        count=count,
        cost=pack.price * count,
        capacity=capacity,
        leftover=max(0.0, capacity - need),
        shortfall=shortfall,
        shortfall_pct=100 * shortfall / need,
        # Per single pack, not per multiple: it is the shelf price comparison,
        # and it does not move with how many you happen to need this week.
        unit_cost=pack.price / capacity_each,
        cost_delta=0.0,
        leftover_delta=0.0,
        quantity_unit=unit,
    )


@dataclass(frozen=True, slots=True)
class Supply:
    """How long a pack lasts, and which of the two clocks ran out first."""

    weeks: float
    #: "expiry" when the date beat you to it, "consumption" when you beat the date.
    limited_by: str


def weeks_of_supply(
    ingredient: Ingredient,
    pack: Pack,
    capacity: float,
    need: float,
    *,
    recipes: int,
    uses: int,
) -> Supply | None:
    """How long a pack of ``capacity`` lasts: whichever runs out first, it or you.

    ``need`` is a week in which the ingredient came up, which is not every week:
    an ingredient in 2% of a five-recipe library turns up about once every ten
    weeks, so a jar holding twenty weeks' worth of a single meal is really four
    years of cupboard. Estimated from the library rather than from your own
    cooking, so it is a scale ("months" against "years"), not a promise.

    Capped by the stated shelf life, because how long you would *take* to eat it
    is only the answer while it is still edible. Four months of mozzarella is two
    weeks of mozzarella and then a fortnight of regret.
    """
    if need <= 0 or uses <= 0 or recipes <= 0 or ingredient.recipe_pct <= 0:
        return None
    expected_uses = (ingredient.recipe_pct / 100.0) * recipes
    weekly_need = need * expected_uses / uses
    if weekly_need <= 0:
        return None
    weeks = capacity / weekly_need
    if pack.shelf_life_days and pack.shelf_life_days / 7.0 < weeks:
        return Supply(weeks=pack.shelf_life_days / 7.0, limited_by="expiry")
    return Supply(weeks=weeks, limited_by="consumption")


def pack_options(
    ingredient: Ingredient,
    cover: Cover,
    need: float,
    *,
    recipes: int = 0,
    uses: int = 1,
    override: str | None = None,
    standing: str | None = None,
    shortfall_tolerance_pct: float = DEFAULT_PACK_SHORTFALL_TOLERANCE_PCT,
) -> tuple[PackOption, ...]:
    """Every size this ingredient could be bought in, priced against the cover.

    Exact and explicitly-approved ``form_differs`` products are offered. True
    substitutes remain a stock fallback, not an ordinary shopping preference.
    """
    unit = cover.quantity_unit
    if need <= 0:
        return ()
    packs: list[Pack] = []
    for match_type in ("exact", "form_differs"):
        packs.extend(drop_poorly_rated(tuple(
            pack for pack in ingredient.available_packs if pack.match_type == match_type
        )))
    for choice in cover.choices:
        if choice.pack not in packs:
            packs.append(choice.pack)
    for held in (pinned_pack(ingredient, standing), pinned_pack(ingredient, override)):
        if held is not None and held not in packs:
            packs.append(held)
    if len(packs) < 2:
        return ()

    chosen_capacity = cover.capacity_qty if unit == "unit" else cover.capacity_g
    chosen_unit_cost = cover.cost / chosen_capacity if chosen_capacity else 0.0
    chosen_skus = {choice.pack.sku for choice in cover.choices}
    chosen_count = cover.choices[0].count if len(cover.choices) == 1 else None

    options: list[PackOption] = []
    for pack in packs:
        full_option = _pack_option(pack, need, unit)
        short_option = _pack_option(
            pack, need, unit, shortfall_tolerance_pct=shortfall_tolerance_pct
        )
        if full_option is None or short_option is None:
            continue
        # Falling short is only a suggestion when it actually lowers this
        # week's spend; otherwise retain the fully-covering version of the SKU.
        usable_short = (
            short_option if short_option.shortfall and short_option.cost < cover.cost else None
        )
        is_current_sku = len(chosen_skus) == 1 and pack.sku in chosen_skus
        if is_current_sku:
            # Keep the actual basket choice beside a possible cheaper count of
            # the same SKU. The latter is an alternative, not already chosen.
            candidates = [full_option]
            if usable_short is not None:
                candidates.append(usable_short)
        else:
            candidates = [usable_short or full_option]
        for option in candidates:
            options.append(replace(
                option,
                cost_delta=option.cost - cover.cost,
                leftover_delta=option.leftover - (cover.leftover_qty
                                                  if unit == "unit" else cover.leftover_g),
                chosen=len(chosen_skus) == 1 and pack.sku in chosen_skus,
                pinned=standing is not None and pack.sku == standing,
                this_week=override is not None and pack.sku == override,
                supply=weeks_of_supply(
                    ingredient, pack, option.capacity, need, recipes=recipes, uses=uses
                ),
            ))

    current = cover.choices[0].pack if len(chosen_skus) == 1 else None
    best = _best_value(options, ingredient, chosen_unit_cost, chosen_capacity or 0.0, current)
    saving = _best_saving(options, current)
    recommendation = saving or best
    options = [
        replace(
            option,
            better_value=best is not None and option == best,
            recommended=option == recommendation,
            recommendation_reason=(
                "different_form_shortfall"
                if option.form_differs and option.shortfall
                else "different_form"
                if option.form_differs
                else "shortfall"
                if option.shortfall
                else "cash_saving"
                if option.cost_delta < 0
                else "better_value"
            ) if option == recommendation else None,
        )
        for option in options
    ]
    options.sort(key=lambda o: (not o.recommended, o.cost, o.unit_cost))
    return tuple(options)


def _best_saving(options: Sequence[PackOption], current: Pack | None) -> PackOption | None:
    candidates = [
        option for option in options
        if not option.chosen
        and option.cost_delta < -1e-9
        and not (current is not None and _rating_downgrade(option.pack, current))
    ]
    return min(
        candidates,
        key=lambda option: (option.cost, option.shortfall_pct, option.form_differs),
        default=None,
    )


def _best_value(
    options: Sequence[PackOption],
    ingredient: Ingredient,
    chosen_unit_cost: float,
    chosen_capacity: float,
    current: Pack | None = None,
) -> PackOption | None:
    """The size worth recommending over the one the planner picked, if any.

    Most of these ask the same question - will the rest of it get eaten? A
    cheaper £/kg on something that spoils, or that this library barely cooks, is
    not a saving but a slower way of throwing money out. The rating is the other
    question: a bulk bag nobody rates above two stars is cheap for a reason.
    """
    if not chosen_unit_cost or ingredient.recipe_pct < BULK_MIN_RECIPE_PCT:
        return None
    candidates = [
        option
        for option in options
        if not option.chosen
        and option.shortfall == 0
        and (current is None or option.pack.match_type == current.match_type)
        and option.keeps
        and option.capacity > chosen_capacity
        and option.unit_cost <= chosen_unit_cost * (1 - BULK_MIN_UNIT_SAVING)
        and option.cost_delta <= BULK_MAX_EXTRA_GBP
        and not (current is not None and _rating_downgrade(option.pack, current))
    ]
    return min(candidates, key=lambda o: o.unit_cost) if candidates else None


@dataclass(frozen=True, slots=True)
class Selection:
    """A recipe in the week's plan, cooked for ``servings`` people.

    ``protein`` is this week's swap or scale for the dish, held with the week
    rather than in the database for the same reason ``pack_overrides`` is: it is
    a decision about one shop, and the recipe it modifies is left as published.
    """

    recipe_id: int
    servings: int | None = None
    protein: ProteinModifier | None = None


@dataclass(frozen=True, slots=True)
class BasketContribution:
    recipe_id: int
    recipe_name: str
    grams: float
    quantity: float | None = None
    quantity_unit: str = "g"


@dataclass(frozen=True, slots=True)
class SnapOption:
    """A small, explicit reduction which avoids buying another pack."""

    original_need_g: float
    snapped_need_g: float
    reduction_pct: float
    saving_gbp: float


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
    options: tuple[PackOption, ...] = ()
    snap: SnapOption | None = None
    snapped: bool = False
    #: What the cupboard supplied toward this line, already subtracted from
    #: ``need_g``/``need_qty`` before the cover was chosen. The full recipe
    #: demand is the sum of the two; kept apart so the page can say "300 g from
    #: the cupboard" without re-deriving it.
    pantry_g: float = 0.0
    pantry_qty: float | None = None

    @property
    def from_pantry(self) -> bool:
        """Some of this line's demand was met without buying anything."""
        return self.pantry_g > 0 or bool(self.pantry_qty)

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
    def upsize(self) -> PackOption | None:
        """A bigger pack worth offering: cheaper per kg, and it will keep."""
        return next((option for option in self.options if option.better_value), None)

    @property
    def pinned_option(self) -> PackOption | None:
        return next((option for option in self.options if option.pinned), None)

    @property
    def trace(self) -> bool:
        """A whole pack bought to satisfy a trace demand - see ``TRACE_NEED_G``.

        A line with no cover bought no pack, so it cannot be one however small
        its demand: a cupboard line sits at a need of zero, and reading that as
        "a whole pack for a pinch" is exactly backwards.
        """
        return (
            self.cover is not None
            and self.unit_kind != "count"
            and self.need_g <= TRACE_NEED_G
        )

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

    @property
    def pantry_lines(self) -> list[BasketLine]:
        """Lines the cupboard made smaller or removed entirely."""
        return [line for line in self.lines if line.from_pantry]


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
        for need in modified_needs(index, recipe, selection.protein):
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


def score_basket(
    index: PlanIndex,
    selections: Iterable[Selection],
    *,
    pack_preferences: dict[str, str] | None = None,
) -> BasketScore:
    """Price a selection without building the itemised basket.

    :func:`build_basket` spends most of its time assembling things a ranking
    never reads — per-line contribution tuples, rounded display quantities, the
    cost-ordered line list. Ranking the library calls this once per candidate and
    then some, so it walks the same decisions and keeps only the totals. It must
    agree with ``build_basket`` exactly; ``test_score_basket_agrees_with_build_basket``
    holds the two together — which is why ``pack_preferences`` is honoured here
    too even though a ranking rarely varies on it.
    """
    pack_preferences = pack_preferences or {}
    needs: dict[str, Demand] = {}
    gaps = 0
    for selection in selections:
        recipe = index.recipes.get(selection.recipe_id)
        if recipe is None:
            continue
        servings = selection.servings or recipe.base_yield
        factor = servings / recipe.base_yield if recipe.base_yield else 1.0
        gaps += recipe.untracked_lines
        for need in modified_needs(index, recipe, selection.protein):
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
        cover = cover_need(
            index,
            ingredient,
            demand.grams,
            demand.units,
            override=pack_preferences.get(key),
        )
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


def _snap_option(
    index: PlanIndex,
    ingredient: Ingredient,
    need_g: float,
    need_units: float | None,
    baseline: Cover | None,
    override: str | None,
    shortfall_tolerance_pct: float,
) -> SnapOption | None:
    """Find the nearest lower whole-pack quantity that saves a pack.

    Snapping is deliberately conservative. It is for weighed ingredients only,
    must remove at least one pack, and can trim no more than the household's
    configured tolerance. Counted ingredients are recipe instructions rather
    than a quantity that can safely be shaved.
    """
    if ingredient.unit_kind == "count" or baseline is None or baseline.packs < 2 or need_g <= 0:
        return None
    candidates: list[SnapOption] = []
    for pack in preferred_packs(ingredient, override=override):
        for count in range(1, baseline.packs):
            target = pack.capacity_g * count
            if target >= need_g:
                continue
            reduction = (need_g - target) / need_g
            if reduction * 100 > shortfall_tolerance_pct:
                continue
            cover = cover_need(index, ingredient, target, need_units, override=override)
            if cover is None or cover.packs >= baseline.packs or cover.cost >= baseline.cost:
                continue
            candidates.append(
                SnapOption(
                    original_need_g=need_g,
                    snapped_need_g=target,
                    reduction_pct=reduction * 100,
                    saving_gbp=baseline.cost - cover.cost,
                )
            )
    return max(candidates, key=lambda option: (option.snapped_need_g, option.saving_gbp), default=None)


def _draw_from_pantry(
    ingredient: Ingredient, demand: Demand, held: Demand
) -> tuple[Demand, Demand | None]:
    """Meet what demand the cupboard can, returning (remaining, drawn).

    Count ingredients are drawn in units with grams scaled alongside for
    display; mass ingredients in grams. The cupboard is a lower bound on what is
    actually there, so drawing up to it never over-promises more than the pantry
    model already does.

    A count draw is capped at whole units of stock, since that is what stock of
    a countable thing comes in. The demand it is met against stays fractional —
    a recipe scaled to three portions really does want 5.94 sausages — so the
    remainder can be fractional too, and the cover ceils it back to whole packs.
    """
    if ingredient.unit_kind == "count":
        if not demand.units or not held.units:
            return demand, None
        take = min(demand.units, float(math.floor(held.units)))
        if take <= 0:
            return demand, None
        fraction = take / demand.units
        drawn = Demand(grams=demand.grams * fraction, units=take)
        return (
            Demand(grams=demand.grams - drawn.grams, units=demand.units - take),
            drawn,
        )
    take_g = min(demand.grams, held.grams)
    if take_g <= 0:
        return demand, None
    return Demand(grams=demand.grams - take_g, units=demand.units), Demand(grams=take_g)


def _demand_met(ingredient: Ingredient, demand: Demand) -> bool:
    """Nothing left to buy once the cupboard has taken its share."""
    if ingredient.unit_kind == "count":
        return (demand.units or 0.0) <= COUNT_CEIL_EPSILON
    return demand.grams <= 0.0


def build_basket(
    index: PlanIndex,
    selections: Iterable[Selection],
    *,
    include_staples: bool = False,
    pack_overrides: dict[str, str] | None = None,
    snap_overrides: dict[str, bool] | None = None,
    pack_preferences: dict[str, str] | None = None,
    pack_shortfall_tolerance_pct: float = DEFAULT_PACK_SHORTFALL_TOLERANCE_PCT,
    pantry: dict[str, Demand] | None = None,
) -> Basket:
    """Price a week's recipes: one pack decision per canonical ingredient.

    ``pack_overrides`` are this week's pack choices, ``{ingredient_key: sku}``,
    so choosing the big bag once is forgotten by the next shop.
    ``pack_preferences`` is the same shape but standing — "I always buy the kilo
    bag" — and comes from the requesting user rather than from the index, which
    is shared. A week's choice beats a standing one.

    Both end up in the same place: the covering only needs to know which pack was
    chosen, so they are resolved into a single sku before it is asked. That also
    keeps them out of :attr:`PlanIndex.cover_cache`'s blind spot — the cache is
    keyed on the chosen sku, so two users with different standing packs get
    different entries rather than each other's.

    ``pantry`` is what the cupboard already holds, ``{ingredient_key: Demand}``
    from :func:`app.pantry.store.read_pantry`, and is spent before anything is
    bought. Per-user like the preferences, and for the same reason — it must
    never reach the shared index. Cover caching is unaffected: a draw only
    changes the demand a cover is asked for, and demand is already in the key.
    """
    selections = list(selections)
    plan_size = len(selections)
    pack_overrides = pack_overrides or {}
    snap_overrides = snap_overrides or {}
    pack_preferences = pack_preferences or {}
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
        line_contributions = tuple(
            BasketContribution(
                recipe_id=c.recipe_id,
                recipe_name=c.recipe_name,
                grams=round(c.grams, 1),
                quantity=round(c.quantity, 3) if c.quantity is not None else None,
                quantity_unit=c.quantity_unit,
            )
            for c in sorted(contributions.get(key, {}).values(), key=lambda c: c.recipe_id)
        )

        # Before the shoppable check, deliberately: an ingredient the cupboard
        # already covers does not need the shop to have any, so being sold out
        # or unpriceable stops being a gap in the week rather than staying one.
        drawn = None
        held = pantry.get(key) if pantry else None
        if held is not None:
            demand, drawn = _draw_from_pantry(ingredient, demand, held)
        if drawn is not None and _demand_met(ingredient, demand):
            # The whole line comes out of the cupboard: no cover, no cost, and
            # the note is what the page shows in place of a pack.
            basket.lines.append(
                BasketLine(
                    key=key,
                    name=label,
                    need_g=0.0,
                    note="in the pantry",
                    unit_kind=ingredient.unit_kind,
                    need_qty=0.0 if ingredient.unit_kind == "count" else None,
                    quantity_unit="unit" if ingredient.unit_kind == "count" else "g",
                    pantry_g=round(drawn.grams, 1),
                    pantry_qty=(
                        round(drawn.units, 3) if drawn.units is not None else None
                    ),
                    contributions=line_contributions,
                )
            )
            continue

        if not ingredient.shoppable:
            if ingredient.sold_out:
                basket.sold_out.append(label)
            else:
                basket.unpriceable.append(label)
            continue

        week_choice = pack_overrides.get(key)
        standing = pack_preferences.get(key)
        override = chosen_sku(week_choice, standing)
        baseline = cover_need(index, ingredient, demand.grams, demand.units, override=override)
        snap = _snap_option(
            index,
            ingredient,
            demand.grams,
            demand.units,
            baseline,
            override,
            pack_shortfall_tolerance_pct,
        )
        snapped = bool(snap and snap_overrides.get(key))
        if snapped:
            demand = Demand(grams=snap.snapped_need_g, units=demand.units)
        cover = cover_need(index, ingredient, demand.grams, demand.units, override=override)
        line = BasketLine(
            key=key,
            name=label,
            need_g=round(demand.grams, 1),
            cover=cover,
            unit_kind=ingredient.unit_kind,
            need_qty=round(demand.units, 3) if demand.units is not None else None,
            quantity_unit="unit" if ingredient.unit_kind == "count" else "g",
            snap=snap,
            snapped=snapped,
            pantry_g=round(drawn.grams, 1) if drawn is not None else 0.0,
            pantry_qty=(
                round(drawn.units, 3)
                if drawn is not None and drawn.units is not None
                else None
            ),
            contributions=line_contributions,
        )
        if cover is None:
            line.note = "no pack covers this demand"
        else:
            # Only ever built here, never in ``score_basket``: a ranking compares
            # totals and would pay for a pack-size menu it never reads.
            line.options = pack_options(
                ingredient,
                cover,
                line.need_qty if line.quantity_unit == "unit" else line.need_g,
                recipes=plan_size,
                uses=len(line.contributions) or 1,
                # Told apart again here: the menu labels a pack "pinned" or "this
                # week", and collapsing them would lose the difference the two
                # decisions are meant to have.
                override=week_choice,
                standing=standing,
                shortfall_tolerance_pct=pack_shortfall_tolerance_pct,
            )
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
