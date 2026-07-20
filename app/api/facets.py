"""Facet configuration: which raw source values become user-facing filters.

The scraped data is noisy — cuisines include non-English labels, and tags mix
useful dietary/attribute signals with internal ``seo`` markers. This module maps
the raw values to a curated, presentable set. It is data, not logic, so it lives
apart from the query code in ``recipes.py``.
"""
from __future__ import annotations

# Only surface cuisines with at least this many curated recipes; the long tail
# is mostly mislabelled or one-offs.
CUISINE_MIN_COUNT = 20

# Fix obvious non-English / malformed cuisine labels that clear the threshold.
CUISINE_RENAME = {
    "Cajunsk": "Cajun",
    "Peruansk": "Peruvian",
    "Malaysisk": "Malaysian",
    "Brasiliansk": "Brazilian",
    "Argentinsk": "Argentinian",
    "Skandinavisk": "Scandinavian",
    "Sri Lankesisk": "Sri Lankan",
    "Indonesisk": "Indonesian",
    "Svensk": "Swedish",
    "Schweizisk": "Swiss",
    "Israelisk": "Israeli",
    "Ungersk": "Hungarian",
    "Jamaicansk": "Jamaican",
    "Zanzibarisk": "Zanzibari",
    "Fusion cuisine": "Fusion",
}

# Dietary filters are backed by derived boolean columns on Recipe (the source's
# own diet tags are too incomplete). value -> (Recipe column name, label).
DIET_COLUMNS = {
    "vegetarian": ("is_vegetarian", "Vegetarian"),
    "pescatarian": ("is_pescatarian", "Pescatarian"),
    "dairy_free": ("is_dairy_free", "Dairy-free"),
    "gluten_free": ("is_gluten_free", "Gluten-free"),
    "low_carb": ("is_low_carb", "Low carb"),
}

# Attribute filters: lighter-weight "nice to have" tags.
ATTRIBUTE_TAGS = {
    "high-protein": "High protein",
    "quick": "Quick",
    "super-quick": "Super quick",
    "family-friendly": "Family friendly",
    "spicy": "Spicy",
    "calorie-smart": "Calorie smart",
    "healthy": "Healthy",
}

# Ingredient-keyword groups used by the "protein" include filter and the
# "exclude" filter. value -> ingredient-name substrings (ILIKE), matched against
# RecipeIngredient.name.
INGREDIENT_KEYWORDS = {
    "chicken": ["chicken"],
    "beef": ["beef", "sirloin", "rump steak", "brisket", "fillet steak", "flank steak"],
    "pork": ["pork", "bacon", "sausage", "chorizo", "gammon", "pancetta", "lardons"],
    "lamb": ["lamb"],
    "turkey": ["turkey"],
    "duck": ["duck"],
    "fish": [
        "fish", "salmon", "cod", "tuna", "haddock", "sea bass", "sea bream", "basa",
        "coley", "tilapia", "whiting", "pollock", "trout", "mackerel", "plaice", "hake",
    ],
    "prawn": ["prawn", "shrimp"],
    "tofu": ["tofu"],
    "halloumi": ["halloumi"],
    "coconut": ["coconut"],
}

# The proteins offered in the "protein" include filter. value -> label.
PROTEIN_FILTERS = {
    "chicken": "Chicken",
    "beef": "Beef",
    "pork": "Pork",
    "lamb": "Lamb",
    "turkey": "Turkey",
    "duck": "Duck",
    "fish": "Fish",
    "prawn": "Prawns",
    "tofu": "Tofu",
    "halloumi": "Halloumi",
}

# Extra ingredient excludes offered alongside allergens in the "exclude" filter.
EXCLUDE_INGREDIENTS = {
    "chicken": "Chicken",
    "beef": "Beef",
    "pork": "Pork",
    "lamb": "Lamb",
    "fish": "Fish",
    "prawn": "Prawns",
    "tofu": "Tofu",
    "coconut": "Coconut",
}

# Sort options exposed to the UI: value -> label.
SORTS = {
    "popular": "Most popular",
    "rating": "Highest rated",
    "protein_high": "Most protein",
    "protein_ratio": "Most protein per calorie",
    "kcal_low": "Fewest calories",
    "time_low": "Quickest",
    "newest": "Newest",
}

DEFAULT_SORT = "popular"


def clean_cuisine(name: str) -> str:
    return CUISINE_RENAME.get(name, name)
