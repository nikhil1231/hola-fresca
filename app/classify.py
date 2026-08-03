"""Per-recipe derived signals: protein density, macro sanity, diet suitability.

These are pure functions over primitive inputs (names, numbers) so they can run
both in the normalizer (over the IR) and in the enrich backfill (over DB rows).
The source's own dietary tags are incomplete, so diet suitability is derived from
the ingredient list, the allergen list and the macros.
"""
from __future__ import annotations

import re
import unicodedata

# --- protein density -------------------------------------------------------

def protein_energy_ratio(protein_g: float | None, energy_kcal: float | None) -> float | None:
    """Grams of protein per 100 kcal, or None if either input is missing/zero."""
    if not protein_g or not energy_kcal or energy_kcal <= 0:
        return None
    return round(protein_g / energy_kcal * 100, 1)


# --- macro sanity ----------------------------------------------------------

def macros_suspect(
    protein_g: float | None,
    carbs_g: float | None,
    fat_g: float | None,
    energy_kcal: float | None,
    tolerance: float = 0.25,
) -> bool:
    """True when Atwater energy (4·P + 4·C + 9·F) diverges from stated kcal.

    Only judges recipes that carry all four numbers; otherwise returns False
    (unknown, not suspect).
    """
    if None in (protein_g, carbs_g, fat_g, energy_kcal) or not energy_kcal:
        return False
    atwater = 4 * protein_g + 4 * carbs_g + 9 * fat_g
    return abs(atwater - energy_kcal) / energy_kcal > tolerance


def macros_implausible_for_veg(is_vegetarian: bool, protein_g: float | None) -> bool:
    """A vegetarian serving realistically tops out ~50g protein.

    Higher values are source data errors (e.g. a protein/carb swap) that the
    Atwater check can't catch because they still reconcile with the energy.
    """
    return bool(is_vegetarian and protein_g and protein_g > 50)


# --- popularity ------------------------------------------------------------

def effective_ratings(
    own_rating: float | None,
    own_count: int | None,
    aggregate_rating: float | None,
    aggregate_count: int | None,
) -> tuple[float | None, int | None]:
    """Pick the rating a recipe should be judged and displayed by.

    Sources version a dish: each revision carries its own rating counters, and a
    revision that never ran long enough sits at zero forever even though the
    dish itself is well established. The aggregate counters span the whole
    lineage and are what the source's own page shows, so they win whenever they
    are the broader sample; the per-revision numbers stay on the row untouched.
    """
    own_n = own_count or 0
    agg_n = aggregate_count or 0
    if agg_n > 0 and agg_n >= own_n:
        return aggregate_rating, agg_n
    return own_rating, (own_count if own_count is not None else None)


# --- diet suitability ------------------------------------------------------

# Meat/fish substitutes: cancel a meat/fish keyword hit ("Plant-Based Mince",
# "Vegan Sausage", "Tofu").
_MEAT_OVERRIDE = re.compile(
    r"\b(?:vegan|vegetarian|plant[-\s]?based|meat[-\s]?free|quorn|tofu|tempeh|"
    r"seitan|jackfruit|beyond|impossible)\b"
)
# Also cancels a dairy hit — plant milks/creams ("Coconut Milk", "Oat Cream").
_DAIRY_OVERRIDE = re.compile(
    r"\b(?:vegan|vegetarian|plant[-\s]?based|meat[-\s]?free|quorn|tofu|tempeh|seitan|"
    r"jackfruit|beyond|impossible|coconut|almond|oat|soya|soy|rice|cashew|hemp)\b"
)
# Cancels a gluten hit — naturally gluten-free bases ("Rice Noodles", "Corn Tortilla").
_GLUTEN_OVERRIDE = re.compile(r"\b(?:rice|corn|gluten[-\s]?free|buckwheat)\b")

_MEAT = {
    "chicken", "beef", "pork", "lamb", "bacon", "sausage", "sausages", "chorizo",
    "ham", "gammon", "prosciutto", "salami", "pepperoni", "turkey", "duck", "veal",
    "mince", "meatball", "meatballs", "steak", "pancetta", "guanciale", "brisket",
    "venison", "rabbit", "goose", "liver", "haggis", "pastrami", "nduja",
    "mortadella", "meat", "lardons", "bratwurst",
}
_FISH = {
    "fish", "salmon", "cod", "tuna", "haddock", "prawn", "prawns", "shrimp",
    "squid", "calamari", "mussel", "mussels", "clam", "anchovy", "anchovies",
    "mackerel", "sardine", "sardines", "crab", "lobster", "scallop", "scallops",
    "seafood", "pollock", "basa", "trout", "seabass", "bass", "bream", "whiting",
    "plaice", "halibut", "herring", "kipper", "whitebait", "octopus", "oyster",
    "roe", "tilapia", "hake", "monkfish", "coley",
}
_DAIRY = {
    "milk", "cheese", "butter", "cream", "yoghurt", "yogurt", "halloumi", "feta",
    "mozzarella", "paneer", "parmesan", "cheddar", "mascarpone", "ricotta", "ghee",
    "custard", "brie", "camembert", "gouda", "gruyere", "emmental", "burrata",
    "quark", "buttermilk", "creme", "fraiche",
}
_GLUTEN = {
    "wheat", "flour", "bread", "breadcrumb", "breadcrumbs", "pasta", "noodle",
    "noodles", "couscous", "freekeh", "bulgur", "bulghur", "barley", "panko",
    "tortilla", "wrap", "wraps", "bun", "buns", "ciabatta", "gnocchi", "orzo",
    "spaghetti", "penne", "linguine", "macaroni", "fusilli", "tagliatelle",
    "rigatoni", "farfalle", "lasagne", "baguette", "naan", "pitta", "pita",
    "brioche", "crouton", "croutons", "pastry", "cracker", "pretzel", "rye",
    "spelt", "semolina",
}
# Allergen names that mean the dish contains gluten.
_GLUTEN_ALLERGENS = {
    "cereals containing gluten", "wheat", "barley", "rye", "oats",
    "spelt (wheat)", "kamut (wheat)", "khorasan (wheat)",
}


def _strip_accents(name: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)
    ).lower()


def _build_pattern(keywords: set[str]) -> re.Pattern:
    # Word-boundary match with an optional regular plural 's', so "Steak" catches
    # "Fillet Steaks" and "Sea Bass" catches "Sea Bass Fillets", while boundaries
    # keep "butter" out of "butternut".
    alts = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    return re.compile(rf"\b(?:{alts})s?\b")


_MEAT_RE = _build_pattern(_MEAT)
_FISH_RE = _build_pattern(_FISH)
_DAIRY_RE = _build_pattern(_DAIRY)
_GLUTEN_RE = _build_pattern(_GLUTEN)


def _matches(name: str, pattern: re.Pattern, override: re.Pattern | None) -> bool:
    ascii_name = _strip_accents(name)
    if override is not None and override.search(ascii_name):
        return False
    return bool(pattern.search(ascii_name))


def diet_flags(
    ingredient_names: list[str],
    allergen_names: list[str],
    carbs_g: float | None,
    energy_kcal: float | None,
) -> dict[str, bool]:
    has_meat = any(_matches(n, _MEAT_RE, _MEAT_OVERRIDE) for n in ingredient_names)
    has_fish = any(_matches(n, _FISH_RE, _MEAT_OVERRIDE) for n in ingredient_names)
    has_dairy_ingredient = any(_matches(n, _DAIRY_RE, _DAIRY_OVERRIDE) for n in ingredient_names)
    has_gluten_ingredient = any(_matches(n, _GLUTEN_RE, _GLUTEN_OVERRIDE) for n in ingredient_names)

    allergens = {a.lower() for a in allergen_names}
    has_milk_allergen = "milk" in allergens
    has_gluten_allergen = bool(allergens & _GLUTEN_ALLERGENS)

    is_vegetarian = not has_meat and not has_fish
    is_pescatarian = not has_meat  # fish allowed; vegetarian dishes qualify too
    is_dairy_free = not has_milk_allergen and not has_dairy_ingredient
    is_gluten_free = not has_gluten_allergen and not has_gluten_ingredient

    is_low_carb = False
    if carbs_g is not None and energy_kcal and energy_kcal > 0:
        is_low_carb = (4 * carbs_g / energy_kcal) < 0.30

    return {
        "is_vegetarian": is_vegetarian,
        "is_pescatarian": is_pescatarian,
        "is_dairy_free": is_dairy_free,
        "is_gluten_free": is_gluten_free,
        "is_low_carb": is_low_carb,
    }


# --- course ----------------------------------------------------------------

MAIN = "main"
SIDE = "side"
BREAKFAST = "breakfast"
DESSERT = "dessert"
PRODUCT = "product"

# Tag types that genuinely name a course. Deliberately narrow: the source also
# has ``lunch-salad``, ``lunch-pasta`` and ``addon-veggie``, which sound like
# accompaniments and are not — a Green Goddess Rump Steak Salad and a Chicken
# and Chorizo Paella carry them, and demoting those would empty the library of
# real dinners.
_SIDE_TAGS = ("sides", "grocery", "light-bites", "game-snacks")
_DESSERT_TAGS = ("dessert",)
_BREAKFAST_TAGS = ("breakfast", "brunch", "busy-mornings")
# Ready meals are sold as a course but cooked by nobody: one line, reheat.
_PRODUCT_TAGS = ("lunch-readymeals",)

# The source writes the course into the title block, as a ``|``-delimited
# segment of the headline ("Starter | with a Sticky Peanut Dipping Sauce") or as
# part of the name ("Rocket & Parmesan Side Salad"). The segments that begin
# with a joining word describe what comes *alongside* instead — "with a Rocket
# Side Salad" hangs off eighty perfectly good dinners — so they are skipped, and
# the name is read only up to its own "with".
_HEADLINE_SEGMENT = re.compile(r"\s*\|\s*")
_ACCOMPANIMENT = re.compile(r"^(?:with|and|plus|served|topped|on a bed|in a)\b")
_NAME_ACCOMPANIMENT = re.compile(r"\bwith\b")

_SIDE_LABEL = re.compile(
    r"\b(?:starter|sharing (?:dish|platter|board)|sides? (?:dish|salad|platter|plate)|"
    r"perfect for sharing|meal addition|pair with|snack|snacking|nibbles|tapas)\b"
)
_DESSERT_LABEL = re.compile(
    r"\b(?:dessert|brownies?|cheesecake|tiramisu|panna cotta|profiteroles?|eton mess|"
    r"sticky toffee|ice cream|gelato|sorbet|sundae|crumble|mousse|churros?|doughnuts?)\b"
)
_BREAKFAST_LABEL = re.compile(
    r"\b(?:breakfast|brunch|smoothie|porridge|granola|overnight oats|parfait)\b"
)

# What makes a plate a dinner: a carbohydrate base, or a portion of protein to
# build the plate around. A dish with neither is an accompaniment however it is
# filed — Garlicky Greens is cavolo nero, garlic and a scattering of lardons.
_BASE = {
    "rice", "basmati", "arborio", "risotto", "pilaf", "biryani", "pasta", "spaghetti",
    "penne", "linguine", "macaroni", "fusilli", "tagliatelle", "rigatoni", "farfalle",
    "fettuccine", "pappardelle", "conchiglie", "casarecce", "bucatini", "paccheri",
    "orzo", "lasagne", "gnocchi", "ravioli", "tortelloni", "tortellini", "girasoli",
    "agnolotti", "pierogi", "noodle", "noodles", "udon", "ramen", "vermicelli",
    "potato", "potatoes", "sweet potato", "sweet potatoes", "mash", "wedges", "fries",
    "chips", "hash brown", "hash browns", "rosti", "dauphinoise", "bread", "sourdough",
    "baguette", "ciabatta", "brioche", "bun", "buns", "bagel", "naan", "pitta", "pita",
    "flatbread", "flatbreads", "wrap", "wraps", "tortilla", "tortillas", "taco",
    "tacos", "nachos", "pizza", "dough", "flour", "pastry", "filo", "croissant",
    "crumpet", "muffin", "muffins", "waffle", "waffles", "pancake", "pancakes",
    "yorkshire", "pie", "couscous", "cous cous", "bulgur", "bulghur", "quinoa",
    "freekeh", "barley", "millet", "semolina", "polenta", "oats", "panko",
    "breadcrumb", "breadcrumbs", "lentil", "lentils", "chickpea", "chickpeas",
    "cannellini", "borlotti", "haricot", "butter bean", "butter beans", "black bean",
    "black beans", "kidney bean", "kidney beans", "mixed beans", "baked beans",
    "refried beans",
}
# Meat substitutes are not cancelled here the way they are for diet flags: the
# question is whether the plate has a portion to build on, and Plant-Based Mince
# is exactly that.
_PORTION = _MEAT | _FISH | {
    "tofu", "halloumi", "paneer", "quorn", "egg", "eggs", "falafel", "tempeh",
    "seitan", "chickpea", "chickpeas", "lentil", "lentils",
}
_BASE_RE = _build_pattern(_BASE)
_PORTION_RE = _build_pattern(_PORTION)

# Per serving. A base is a base at 20 g dry weight; a portion of protein is the
# 120–150 g the source plates a dinner with, well clear of the 45 g of lardons
# that season a bowl of greens.
_MIN_BASE_G = 20.0
_MIN_PORTION_G = 60.0
_DEFAULT_SERVINGS = 2


def _course_labels(name: str, headline: str | None) -> list[str]:
    """The parts of the title block that describe the dish rather than its sides."""
    labels = [_NAME_ACCOMPANIMENT.split((name or "").lower(), maxsplit=1)[0]]
    for segment in _HEADLINE_SEGMENT.split((headline or "").lower()):
        segment = segment.strip()
        if segment and not _ACCOMPANIMENT.match(segment):
            labels.append(segment)
    return labels


def _plate(
    ingredients: list[tuple[str, float | None]], servings: int | None
) -> tuple[bool, bool]:
    """Whether the recipe has a carbohydrate base and a portion of protein.

    A missing or zero amount means the source never quantified that ingredient,
    not that there is none of it — a Chicken Biryani whose rice and thighs both
    read 0 g is still a biryani — so an unquantified base or portion counts.
    """
    base_g = portion_g = 0.0
    base_unquantified = portion_unquantified = False
    for ingredient_name, grams in ingredients:
        ascii_name = _strip_accents(ingredient_name)
        if _BASE_RE.search(ascii_name):
            if grams:
                base_g += grams
            else:
                base_unquantified = True
        if _PORTION_RE.search(ascii_name):
            if grams:
                portion_g += grams
            else:
                portion_unquantified = True
    per_serving = servings or _DEFAULT_SERVINGS
    return (
        base_unquantified or base_g / per_serving >= _MIN_BASE_G,
        portion_unquantified or portion_g / per_serving >= _MIN_PORTION_G,
    )


def course(
    tag_types: list[str] | None,
    ingredients: list[tuple[str, float | None]],
    *,
    name: str = "",
    headline: str | None = None,
    servings: int | None = None,
) -> str:
    """Classify a recipe as a main, side, breakfast, dessert or bought product.

    ``ingredients`` is ``(name, grams for the whole recipe)`` per ingredient.

    Three layers, weakest evidence last. A recipe with one ingredient and
    nothing to do to it is an item you buy — houmous, a garlic baguette, a tub
    of chips — whatever the source files it under. Then the source's own word
    for the dish, from its tags and from the course it prints in the title
    block. Only when nothing has been declared does the plate decide, and it
    decides on structure rather than on calories: the calorie figures are
    per-serving on some rows and per-100 g on others, so they put a 103 kcal
    risotto next to a 484 kcal smoothie and cannot separate the two.
    """
    types = [(t or "").lower() for t in (tag_types or [])]

    if len(ingredients) <= 1:
        return DESSERT if any(t.startswith(_DESSERT_TAGS) for t in types) else PRODUCT
    if any(t.startswith(_PRODUCT_TAGS) for t in types):
        return PRODUCT
    if any(t.startswith(_DESSERT_TAGS) for t in types):
        return DESSERT
    if any(t.startswith(_BREAKFAST_TAGS) for t in types):
        return BREAKFAST
    # No ingredient-count gate on the side tags: the source uses them on
    # nine-ingredient sharing platters and starters just as readily as on a tub
    # of slaw, and it is only wrong the other way round about once in a library
    # (a Bacon and Sweet Potato Risotto tagged ``sides-bread``).
    if any(t.startswith(_SIDE_TAGS) for t in types):
        return SIDE

    for label in _course_labels(name, headline):
        if _DESSERT_LABEL.search(label):
            return DESSERT
        if _BREAKFAST_LABEL.search(label):
            return BREAKFAST
        if _SIDE_LABEL.search(label):
            return SIDE

    has_base, has_portion = _plate(ingredients, servings)
    return MAIN if has_base or has_portion else SIDE
