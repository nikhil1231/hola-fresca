"""How much of a past shop is still worth counting on.

The planner buys packs and cooks quantities, so a week always ends with
remainders. :mod:`app.planner.waste` already prices those remainders; this
decides whether to *spend* them — whether next week's demand can be met out of
the cupboard instead of out of the trolley.

The whole difficulty is that purchases are observed exactly and consumption
never is. A push is a real event with real quantities; whether the chilli
actually got made on Wednesday is a guess, and no amount of modelling turns it
into anything else. So error only ever accumulates in one direction — the
cupboard is always at most what is believed, never more — and three blunt rules
keep that bounded rather than growing:

**Only the cupboard is admitted.** Nothing below :data:`PANTRY_MIN_SALVAGE`
enters. This sounds like caution and is really arithmetic: the ingredients whose
stock would drift fastest are the chiller and the bakery, and those are exactly
the ones the waste model already scores at ~0 by the next shop. Excluding them
gives up almost no money and removes almost all of the error, which leaves the
pantry holding rice, tins, pasta, spices, oil and frozen goods — the things that
genuinely sit in a cupboard for a month.

**Everything decays per shop, not per day.** A cadence is what a household
actually runs on, and silence between shops is itself evidence: someone who did
not shop for a fortnight either ate through the cupboard or ate out, and both
mean there is less there than the model thinks. Decaying on the cadence is
therefore the honest direction, not a fudge.

**A belief that is old enough is dropped, not decayed further.**
:data:`TRUST_HORIZON_CYCLES` is what stops a 5 kg sack of flour bought once from
suppressing flour purchases for a year at a slowly shrinking figure. Every wrong
belief gets a finite lifetime, and a pantry left to go stale ends up empty —
which is to say it degrades to how the planner behaved before it existed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from app import schedule as sched

#: Below this, an ingredient never enters the cupboard. It is the same salvage
#: figure :mod:`app.planner.waste` scores leftovers on, so the boundary is drawn
#: on a scale the planner already trusts rather than on a second opinion:
#: ``_AMBIENT`` (0.85), frozen (0.90) and spices are comfortably above it,
#: ``_BAKERY`` (0.20) and ``_CHILLED`` (0.15) comfortably below.
PANTRY_MIN_SALVAGE = 0.5

#: Shops a lot may go unconfirmed before it is dropped outright.
TRUST_HORIZON_CYCLES = 4

#: Held quantities below this are not worth a line on the page or a subtraction
#: in the basket — a stray gram of rice should not read as "you have rice".
NEGLIGIBLE_G = 5.0
NEGLIGIBLE_QTY = 0.5


@dataclass(frozen=True, slots=True)
class Quantity:
    """An amount of one ingredient, in whichever space the planner counts it in.

    ``grams`` is always populated; ``units`` only for count ingredients, mirroring
    :class:`app.planner.basket.Demand` so the two can be subtracted directly.
    """

    grams: float = 0.0
    units: float | None = None

    def __bool__(self) -> bool:
        if self.units is not None:
            return self.units >= NEGLIGIBLE_QTY
        return self.grams >= NEGLIGIBLE_G

    def scaled(self, factor: float) -> "Quantity":
        return Quantity(
            grams=self.grams * factor,
            units=None if self.units is None else self.units * factor,
        )

    def minus(self, other: "Quantity") -> "Quantity":
        return Quantity(
            grams=max(0.0, self.grams - other.grams),
            units=(
                None
                if self.units is None
                else max(0.0, self.units - (other.units or 0.0))
            ),
        )


def admits(salvage: float | None, *, pantry_staple: bool = False) -> bool:
    """Whether an ingredient's leftovers may be carried to the next shop.

    Staples are excluded because they are already assumed owned outright — the
    basket does not buy them, so there is no purchase to have a remainder of.
    Modelling their diffuse, unplannable consumption is a separate problem from
    this one.
    """
    if pantry_staple or salvage is None:
        return False
    return salvage >= PANTRY_MIN_SALVAGE


def cycles_between(earlier: str, later: str, *, cadence_weeks: int) -> int:
    """Whole shops from one week to another, floored at zero.

    Counts the household's own rhythm rather than calendar weeks: a fortnightly
    shopper's leftovers get one cycle of decay between shops, not two, because
    one shop is what they have to survive.
    """
    try:
        start = sched.parse_date(earlier)
        end = sched.parse_date(later)
    except ValueError:
        return 0
    weeks = (end - start).days // 7
    return max(0, weeks // max(1, cadence_weeks))


def parse_contributions(raw: str | None) -> dict[int, Quantity]:
    """``{recipe_id: Quantity}`` from a lot's stored blob, tolerating rubbish.

    A blob that will not parse costs the *credit* for uncooked recipes, not the
    lot: the cupboard still holds what it holds. Better to under-state what a
    week put back than to drop a real bag of rice over a serialisation change.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[int, Quantity] = {}
    for key, value in parsed.items():
        try:
            recipe_id = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        grams = value.get("g")
        units = value.get("qty")
        out[recipe_id] = Quantity(
            grams=float(grams) if isinstance(grams, (int, float)) else 0.0,
            units=float(units) if isinstance(units, (int, float)) else None,
        )
    return out


def dump_contributions(contributions: dict[int, Quantity]) -> str:
    return json.dumps(
        {
            str(recipe_id): {"g": round(quantity.grams, 3), "qty": quantity.units}
            for recipe_id, quantity in sorted(contributions.items())
        }
    )


def remaining(
    *,
    available: Quantity,
    contributions: dict[int, Quantity],
    cooked_recipe_ids: set[int],
) -> Quantity:
    """What is left after the week's cooked recipes have taken their share.

    Derived rather than stored, which is the point of keeping the contributions:
    unticking a recipe on the Past recipes page puts its grams straight back
    without anything having to be recomputed or rewritten.
    """
    used = Quantity(
        grams=sum(
            contributions[recipe_id].grams
            for recipe_id in cooked_recipe_ids
            if recipe_id in contributions
        ),
        units=(
            sum(
                contributions[recipe_id].units or 0.0
                for recipe_id in cooked_recipe_ids
                if recipe_id in contributions
            )
            if available.units is not None
            else None
        ),
    )
    return available.minus(used)


def decay(quantity: Quantity, *, salvage: float, cycles: int) -> Quantity:
    """A remainder aged by ``cycles`` shops, or nothing past the trust horizon."""
    if cycles >= TRUST_HORIZON_CYCLES:
        return Quantity(grams=0.0, units=None if quantity.units is None else 0.0)
    if cycles <= 0:
        return quantity
    return quantity.scaled(max(0.0, min(1.0, salvage)) ** cycles)


@dataclass(frozen=True, slots=True)
class Lot:
    """A :class:`app.db.models.PantryLot` in the terms this module reasons in."""

    ingredient_key: str
    ingredient_name: str | None
    week_start: str
    available: Quantity
    salvage: float
    contributions: dict[int, Quantity]
    unit_kind: str = "mass"
    emptied: bool = False
    confirmed_week_start: str | None = None

    @property
    def counts_from(self) -> str:
        """The week decay is measured from: the last confirmation, else the shop.

        Saying "yes, that is still there" is the strongest evidence the pantry
        ever gets, and it is worth more than the guess it replaces — so it
        restarts the clock rather than merely nudging the figure.
        """
        return self.confirmed_week_start or self.week_start


def held(
    lot: Lot,
    *,
    cooked_recipe_ids: set[int],
    target_week: str,
    cadence_weeks: int,
) -> Quantity:
    """What may be spent against ``target_week``'s demand, decay included."""
    if lot.emptied:
        return Quantity(grams=0.0, units=None if lot.available.units is None else 0.0)
    left = remaining(
        available=lot.available,
        contributions=lot.contributions,
        cooked_recipe_ids=cooked_recipe_ids,
    )
    cycles = cycles_between(lot.counts_from, target_week, cadence_weeks=cadence_weeks)
    return decay(left, salvage=lot.salvage, cycles=cycles)


def is_stale(lot: Lot, *, today: date | None = None, cadence_weeks: int) -> bool:
    """Past the trust horizon measured from now, so worth deleting rather than reading."""
    now = sched.format_date(sched.week_start_for(today or date.today()))
    return (
        cycles_between(lot.counts_from, now, cadence_weeks=cadence_weeks)
        >= TRUST_HORIZON_CYCLES
    )
