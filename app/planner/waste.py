"""Perishability model: turns leftover pack fractions into a £ figure.

The planner optimises money thrown away, not grams left over — 300 g of spare
basmati and 300 g of spare coriander are not the same mistake. So every leftover
is valued by how much of it survives to be *used*, which on a weekly plan means
"will this still be good at the next shop?".

Ocado states a guaranteed minimum life on delivery for perishables and states
nothing at all for ambient stock, so ``shelf_life_days`` carries the signal and
``category`` covers its NULLs (in the approved mappings: 46 of 61 tinned beans
have no stated life, against 2 of 33 chorizo). Freezer goods keep almost all
their value whatever life is printed on them.

The constants below are deliberately blunt and meant to be re-tuned once real
baskets have been cooked and the leftovers actually observed.
"""
from __future__ import annotations

# Days until the next shop, i.e. how long a leftover must survive to be worth
# anything. Everything below is calibrated against this horizon.
REPLAN_HORIZON_DAYS = 7

# Stated shelf life (days, inclusive upper bound) -> fraction of the remainder
# still worth something at the next shop.
SALVAGE_BY_SHELF_LIFE: tuple[tuple[int, float], ...] = (
    (3, 0.00),   # herbs, bagged salad, fresh fish: gone before they are wanted again
    (7, 0.15),   # only just reaches the horizon, and past its best by then
    (14, 0.40),
    (30, 0.65),
)
SALVAGE_LONG_LIFE = 0.85  # stated life beyond a month

# Fallbacks when the retailer states no life at all, keyed on the category root.
SALVAGE_BY_CATEGORY: dict[str, float] = {
    "Frozen Food": 0.90,
    "Food Cupboard": 0.85,
    "Soft Drinks, Tea & Coffee": 0.85,
    "Dietary, Lifestyle & World Foods": 0.80,
    "Treats & Snacks": 0.75,
    "Bakery": 0.20,  # ambient but stales fast
    # A missing life here means the data is thin, not that the food keeps: these
    # are chiller-aisle goods and they behave like the ~7-day bucket. Without
    # this they fall to SALVAGE_UNKNOWN and score as *better* keepers than
    # identical products that do state a week's life, which is backwards.
    "Fresh & Chilled Food": 0.15,
}
SALVAGE_UNKNOWN = 0.50  # no stated life and no useful category

# A pack is never a total loss: whatever the model says, buying a jar you half
# use is not as bad as burning the money, and nothing is ever fully recovered.
SALVAGE_FLOOR = 0.0
SALVAGE_CEILING = 0.90


def category_root(category: str | None) -> str | None:
    """First segment of an Ocado category path ("Frozen Food > Fish > ...")."""
    if not category:
        return None
    return category.split(">")[0].strip() or None


def salvage_fraction(shelf_life_days: int | None, category: str | None) -> float:
    """Fraction of an unused remainder that still has value at the next shop."""
    root = category_root(category)
    # Frozen wins over any stated life: a stated 2-day life on a freezer product
    # is about the thaw, and the planner is not going to thaw it early.
    if root == "Frozen Food":
        return SALVAGE_BY_CATEGORY[root]
    if shelf_life_days is not None:
        for limit, fraction in SALVAGE_BY_SHELF_LIFE:
            if shelf_life_days <= limit:
                return fraction
        return SALVAGE_LONG_LIFE
    if root in SALVAGE_BY_CATEGORY:
        return SALVAGE_BY_CATEGORY[root]
    return SALVAGE_UNKNOWN


def clamp_salvage(value: float) -> float:
    return max(SALVAGE_FLOOR, min(SALVAGE_CEILING, value))


def waste_value(leftover_g: float, cost: float, capacity_g: float, salvage: float) -> float:
    """Cash value of ``leftover_g`` left over from packs costing ``cost``.

    Prices the remainder at what it cost per gram, then credits back the share
    that will keep until the next shop.
    """
    if leftover_g <= 0 or capacity_g <= 0:
        return 0.0
    return (leftover_g / capacity_g) * cost * (1.0 - clamp_salvage(salvage))
