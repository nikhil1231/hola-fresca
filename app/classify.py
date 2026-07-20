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
