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

Other retailers state far less. Sainsbury's publishes a life for only 7.5% of its
range and shelves by leaf aisle rather than under a storage class, so the
category has to carry nearly all of the signal there — see
:data:`SALVAGE_KEYWORDS_BY_RETAILER`, which is per-retailer because a word only
means one thing inside one shop's vocabulary.

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

# Named for the tables below, so a row reads as a claim about the food rather
# than as a number. The values match their SALVAGE_BY_CATEGORY equivalents.
_AMBIENT = 0.85
_TREATS = 0.75
_BAKERY = 0.20
_CHILLED = 0.15

#: Keywords that only mean one thing *within one retailer's* taxonomy.
#:
#: Kept apart from SALVAGE_BY_KEYWORD because a word's safety depends on the
#: vocabulary it is read in, and the tables above are Ocado's. "Fresh" is the
#: clearest case: every Sainsbury's category containing it is genuinely a chiller
#: aisle, while on Ocado it also names brand ranges — which is exactly why it was
#: excluded from the shared table. Scoping the word to one retailer is what makes
#: it usable at all.
#:
#: This exists because Sainsbury's states a shelf life for only 7.5% of its range
#: against Ocado's 33.4%, and its categories are leaf aisles ("Pulses & beans")
#: rather than a path rooted in a storage class. Without these, 73% of Sainsbury's
#: products fell through to SALVAGE_UNKNOWN and the planner priced three quarters
#: of that catalogue as if half of every leftover survived the week.
#:
#: **Order matters — first match wins.** The entries that look redundant are the
#: ones carrying the exceptions: "peanut butter" is ambient and has to be settled
#: before "butter" reaches the chiller, and "salad cream" is a jar of mayonnaise
#: rather than anything that was ever in a field.
#:
#: **Where a word is genuinely ambiguous, guess low.** Understating how well
#: something keeps makes the planner buy the smaller pack; overstating it makes
#: the planner buy a big bag of something that rots. The second mistake costs
#: money and the first only forgoes a saving, so the tie goes to the chiller.
SALVAGE_KEYWORDS_BY_RETAILER: dict[str, tuple[tuple[str, float], ...]] = {
    "sainsburys": (
        # -- exceptions first, or the general rules below swallow them ---------
        ("peanut butter", _AMBIENT),      # before "butter"
        ("nut butter", _AMBIENT),         # before "butter"
        ("salad cream", _AMBIENT),        # before "salad"
        ("salad dressing", _AMBIENT),     # before "salad"
        ("pulses", _AMBIENT),             # "Pulses & beans" is the tinned aisle
        ("tinned", _AMBIENT),             # before "tomatoes", "fruit", "fish"
        ("canned", _AMBIENT),             # ditto; Sainsbury's uses both words
        ("dried", _AMBIENT),              # before "herb", "fruit"
        ("long life", _AMBIENT),
        # -- the chiller ------------------------------------------------------
        # Safe here in a way it is not on Ocado; see the note above.
        ("fresh", _CHILLED),
        ("chilled", _CHILLED),
        ("yogurt", _CHILLED),
        ("yoghurt", _CHILLED),
        ("cheese", _CHILLED),
        ("butter", _CHILLED),
        ("milk", _CHILLED),
        ("egg", _CHILLED),
        ("ready meal", _CHILLED),
        ("sandwich", _CHILLED),
        ("houmous", _CHILLED),
        ("coleslaw", _CHILLED),
        ("quiche", _CHILLED),
        ("continental meats", _CHILLED),
        ("pizza", _CHILLED),
        # -- meat and fish, which are the shortest-lived things in a basket ----
        ("beef", _CHILLED),
        ("chicken", _CHILLED),
        ("pork", _CHILLED),
        ("lamb", _CHILLED),
        ("turkey", _CHILLED),
        ("bacon", _CHILLED),
        ("sausage", _CHILLED),
        ("mince", _CHILLED),
        ("salmon", _CHILLED),
        ("prawn", _CHILLED),
        ("fish", _CHILLED),
        # -- produce ----------------------------------------------------------
        ("vegetable", _CHILLED),
        ("salad", _CHILLED),
        ("lettuce", _CHILLED),
        ("tomato", _CHILLED),
        ("potato", _CHILLED),
        ("onion", _CHILLED),
        ("pepper", _CHILLED),
        ("mushroom", _CHILLED),
        ("herb", _CHILLED),
        ("apple", _CHILLED),
        ("orange", _CHILLED),
        ("banana", _CHILLED),
        ("berries", _CHILLED),
        ("grape", _CHILLED),
        ("citrus", _CHILLED),
        ("fruit & veg", _CHILLED),
        ("prepped", _CHILLED),
        # -- bakery: ambient, but stales inside the week -----------------------
        ("bread", _BAKERY),
        ("bakery", _BAKERY),
        ("roll", _BAKERY),
        ("wrap", _BAKERY),
        ("pitta", _BAKERY),
        ("naan", _BAKERY),
        ("cake", _BAKERY),
        ("pastr", _BAKERY),          # pastry, pastries
        # Plural deliberately: bare "pie" also matches "pieces", which is a cut
        # of something fresh rather than anything from the bakery.
        ("pies", _BAKERY),
        ("burger", _CHILLED),
        # -- treats: ambient, but nobody plans a week around them --------------
        ("crisps", _TREATS),
        ("chocolate", _TREATS),
        ("sweets", _TREATS),
        ("biscuit", _TREATS),
        ("cracker", _TREATS),
        ("cereal bar", _TREATS),
        ("chewing gum", _TREATS),
        ("mints", _TREATS),
        # -- the cupboard ------------------------------------------------------
        ("pasta", _AMBIENT),
        ("spaghetti", _AMBIENT),
        ("penne", _AMBIENT),
        ("noodle", _AMBIENT),
        ("rice", _AMBIENT),
        ("couscous", _AMBIENT),
        ("lentil", _AMBIENT),
        ("chickpea", _AMBIENT),
        ("flour", _AMBIENT),
        ("sugar", _AMBIENT),
        ("salt", _AMBIENT),
        ("seasoning", _AMBIENT),
        ("stock", _AMBIENT),
        ("oil", _AMBIENT),
        ("vinegar", _AMBIENT),
        ("sauce", _AMBIENT),
        ("paste", _AMBIENT),
        ("marinade", _AMBIENT),
        ("chutney", _AMBIENT),
        ("pickle", _AMBIENT),
        ("relish", _AMBIENT),
        ("ketchup", _AMBIENT),
        ("mustard", _AMBIENT),
        ("mayonnaise", _AMBIENT),
        ("condiment", _AMBIENT),
        ("honey", _AMBIENT),
        ("jam", _AMBIENT),
        ("spread", _AMBIENT),
        ("olives", _AMBIENT),
        ("antipasti", _AMBIENT),
        ("nuts", _AMBIENT),
        ("seeds", _AMBIENT),
        ("cereal", _AMBIENT),
        ("granola", _AMBIENT),
        ("soup", _AMBIENT),
        ("coffee", _AMBIENT),
        ("tea", _AMBIENT),
        ("water", _AMBIENT),
        ("squash", _AMBIENT),
        ("lemonade", _AMBIENT),
        ("juice", _AMBIENT),
        ("mixer", _AMBIENT),
        ("wine", _AMBIENT),
        ("beer", _AMBIENT),
    ),
}

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


def category_salvage(category: str | None, retailer: str | None = None) -> float | None:
    """What the shelving says about keeping, or None if it says nothing useful.

    Walks from the most specific segment outwards, so "M&S Food Cupboard,
    Bakery & Drinks > M&S Bakery" comes out as bakery rather than cupboard.

    ``retailer`` selects the taxonomy-specific keywords, which are tried before
    the shared ones — a word can only be read against the vocabulary it was
    written in. Omitting it falls back to the shared table alone, which is what
    the Ocado-era callers have always got.
    """
    keywords = SALVAGE_KEYWORDS_BY_RETAILER.get(retailer or "", ()) + SALVAGE_BY_KEYWORD
    for segment in _segments(category):
        if segment in SALVAGE_BY_CATEGORY:
            return SALVAGE_BY_CATEGORY[segment]
        lowered = segment.lower()
        for keyword, fraction in keywords:
            if keyword in lowered:
                return fraction
    return None


def is_frozen(category: str | None) -> bool:
    """Only the word itself: a caramel sauce shelved in the ice cream aisle is
    not frozen, and this answer overrides a stated shelf life."""
    return any("frozen" in segment.lower() for segment in _segments(category))


def salvage_fraction(
    shelf_life_days: int | None, category: str | None, retailer: str | None = None
) -> float:
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
    from_category = category_salvage(category, retailer)
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
