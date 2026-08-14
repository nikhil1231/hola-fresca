"""Per-retailer salvage keywords: what a category says about keeping.

Sainsbury's states a shelf life for 7.5% of its range against Ocado's 33.4%, and
shelves by leaf aisle ("Pulses & beans") rather than under a storage class. Left
to the Ocado-shaped tables, 73% of the Sainsbury's catalogue fell through to
SALVAGE_UNKNOWN — the planner pricing three quarters of a shop as if half of
every leftover survived the week.
"""
from __future__ import annotations

import pytest

from app.planner import waste

AMBIENT = 0.85
TREATS = 0.75
BAKERY = 0.20
CHILLED = 0.15


def salvage(category, retailer="sainsburys"):
    return waste.salvage_fraction(None, category, retailer)


# --- the shared table stays Ocado's ------------------------------------------


def test_sainsburys_keywords_do_not_leak_into_ocado():
    """The whole reason the table is per-retailer.

    "Fresh" is safe in Sainsbury's vocabulary and unsafe in Ocado's, where it
    also names brand ranges — so scoping is what makes the word usable at all.
    """
    assert salvage("Fresh pasta", "sainsburys") == CHILLED
    assert waste.salvage_fraction(None, "Fresh pasta", "ocado") == waste.SALVAGE_UNKNOWN


def test_an_unknown_retailer_gets_the_shared_table_only():
    # Including the no-retailer call, which is what every pre-existing caller made.
    for retailer in (None, "waitrose"):
        assert waste.salvage_fraction(None, "Fresh pasta", retailer) == waste.SALVAGE_UNKNOWN
        assert waste.salvage_fraction(None, "Frozen Food > Peas", retailer) == 0.90


def test_the_shared_rules_still_apply_to_sainsburys():
    # Retailer keywords are tried first, then the shared ones — not instead.
    assert salvage("Frozen peas, beans & sweetcorn") == 0.90
    assert salvage("Asian food cupboard") == 0.85           # shared "food cupboard"
    assert salvage("Indian curry pastes > Asian world foods") == 0.80  # shared "world foods"


# --- ordering: the exceptions that make the general rules safe ---------------


@pytest.mark.parametrize(
    "category,expected,why",
    [
        ("Peanut butter", AMBIENT, "a jar of peanut butter is not dairy"),
        ("Block butter", CHILLED, "but actual butter is"),
        ("Salad cream & mayonnaise", AMBIENT, "salad cream never saw a field"),
        ("Salad dressings", AMBIENT, "nor did dressing"),
        ("Salad bags", CHILLED, "bagged salad is the shortest-lived thing there is"),
        ("Tinned tomatoes", AMBIENT, "tinned beats the produce word"),
        ("Tomatoes", CHILLED, "loose tomatoes do not"),
        ("Dried herbs", AMBIENT, "dried beats the produce word"),
        ("Fresh herbs", CHILLED, "fresh herbs are gone within days"),
        ("Dried fruit and peel", AMBIENT, "dried beats fruit"),
        ("Pulses & beans", AMBIENT, "the tinned-pulses aisle, not a vegetable"),
        ("Fresh pasta sauce", CHILLED, "fresh is checked before sauce"),
        ("Italian pasta sauces", AMBIENT, "a jar of sauce keeps"),
        ("Long life orange juice", AMBIENT, "long life says so outright"),
    ],
)
def test_ordering_exceptions(category, expected, why):
    assert salvage(category) == expected, why


# --- the buckets themselves ---------------------------------------------------


@pytest.mark.parametrize(
    "category,expected",
    [
        ("Basmati rice", AMBIENT),
        ("Olive oil", AMBIENT),
        ("Stocks", AMBIENT),
        ("Honey", AMBIENT),
        ("Cheddar and British regional cheese", CHILLED),
        ("Natural, organic and greek yogurt", CHILLED),
        ("Continental meats", CHILLED),
        ("Chicken wings, thighs and drumsticks", CHILLED),
        ("Potatoes & sweet potatoes", CHILLED),
        ("Lettuce", CHILLED),
        ("Bread rolls", BAKERY),
        ("Sharing crisps", TREATS),
        ("Chocolate bars", TREATS),
    ],
)
def test_aisles_land_in_the_right_bucket(category, expected):
    assert salvage(category) == expected


def test_pie_is_matched_as_a_plural_so_it_cannot_swallow_pieces():
    # Bare "pie" also matches "pieces", which is a cut of something fresh.
    assert salvage("Pies") == BAKERY
    assert salvage("Chicken breast pieces") == CHILLED


# --- how a category path is read ---------------------------------------------


def test_the_most_specific_segment_decides():
    # Sainsbury's categories arrive as a list of tags; the adapter joins them
    # with the storage label first, so the aisle behind it has to win.
    assert salvage("Fresh & Chilled Food > Continental meats") == CHILLED
    assert salvage("Fresh & Chilled Food > Long life orange juice") == AMBIENT


def test_a_tag_that_says_nothing_falls_through_to_the_aisle():
    # "Vegetarian & plant based" is a dietary tag appended after the real aisle,
    # and matches no keyword — so the aisle behind it still decides.
    assert salvage("Coleslaw & salads > Vegetarian & plant based") == CHILLED
    assert salvage("Nuts > Vegetarian & plant based") == AMBIENT


def test_a_stated_shelf_life_still_beats_the_category():
    # The keywords are a fallback for NULLs, not an override.
    assert waste.salvage_fraction(2, "Basmati rice", "sainsburys") == 0.0
    assert waste.salvage_fraction(400, "Fresh herbs", "sainsburys") == waste.SALVAGE_LONG_LIFE


def test_frozen_still_overrides_everything():
    assert waste.salvage_fraction(2, "Frozen peas, beans & sweetcorn", "sainsburys") == 0.90


def test_nothing_recognisable_is_still_honestly_unknown():
    # Non-food and bare tags should stay at the default rather than being guessed.
    assert salvage("Womens deodorants and sprays") == waste.SALVAGE_UNKNOWN
    assert salvage(None) == waste.SALVAGE_UNKNOWN
