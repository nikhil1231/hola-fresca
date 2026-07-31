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

# Fallbacks when the retailer states no life at all, matched against the aisle
# words anywhere in the category path.
#
# Matched on *words in any segment* rather than on the first one, because Ocado
# shelves a good deal of its range under a brand or a cuisine instead of an
# aisle: "M&S > M&S Food Cupboard, Bakery & Drinks > M&S Food Cupboard" and
# "Ocado Own Range > Bakery & Food Cupboard > Food Cupboard" are both ambient
# groceries, and reading only the root filed them under "no idea" — so two
# identical bags of sugar came out with different keeping qualities, and the
# planner preferred the dearer one. Spices arrive the same way, shelved by
# cuisine ("Indian Spices", "Cajun Spices"), and those are the very products
# where buying the bigger jar is most obviously right.
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

#: Substring -> salvage, tried against each segment. Deliberately short, and
#: deliberately only words that mean one thing in this taxonomy: "bakery" is not
#: here because Ocado files baking sugar under "M&S Bakery", and "fresh" is not
#: here because "M&S Best of Fresh" is a brand range rather than a chiller. An
#: ingredient whose shelving is that ambiguous is better left at SALVAGE_UNKNOWN
#: than confidently mis-scored.
SALVAGE_BY_KEYWORD: tuple[tuple[str, float], ...] = (
    ("frozen", 0.90),
    ("spice", 0.85),
    ("food cupboard", 0.85),
    ("world foods", 0.80),
)

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


def _segments(category: str | None) -> list[str]:
    """Path segments, deepest first - the deepest is the most specific shelf."""
    if not category:
        return []
    return [part.strip() for part in reversed(category.split(">")) if part.strip()]


def category_salvage(category: str | None) -> float | None:
    """What the shelving says about keeping, or None if it says nothing useful.

    Walks from the most specific segment outwards, so "M&S Food Cupboard,
    Bakery & Drinks > M&S Bakery" comes out as bakery rather than cupboard.
    """
    for segment in _segments(category):
        if segment in SALVAGE_BY_CATEGORY:
            return SALVAGE_BY_CATEGORY[segment]
        lowered = segment.lower()
        for keyword, fraction in SALVAGE_BY_KEYWORD:
            if keyword in lowered:
                return fraction
    return None


def is_frozen(category: str | None) -> bool:
    """Only the word itself: a caramel sauce shelved in the ice cream aisle is
    not frozen, and this answer overrides a stated shelf life."""
    return any("frozen" in segment.lower() for segment in _segments(category))


def salvage_fraction(shelf_life_days: int | None, category: str | None) -> float:
    """Fraction of an unused remainder that still has value at the next shop."""
    # Frozen wins over any stated life: a stated 2-day life on a freezer product
    # is about the thaw, and the planner is not going to thaw it early.
    if is_frozen(category):
        return SALVAGE_BY_CATEGORY["Frozen Food"]
    if shelf_life_days is not None:
        for limit, fraction in SALVAGE_BY_SHELF_LIFE:
            if shelf_life_days <= limit:
                return fraction
        return SALVAGE_LONG_LIFE
    from_category = category_salvage(category)
    return SALVAGE_UNKNOWN if from_category is None else from_category


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
