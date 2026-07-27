"""Cooking-facing quantities: spoon translation, gram provenance, potency.

The point of these is that the source's own unit outranks our derived weight, so
what is worth pinning down is which lines get a translation offered, which get
their gram figure marked as ours rather than HelloFresh's, and which are potent
enough that a wrong estimate is worth flagging to the cook.
"""
from __future__ import annotations

import pytest

from app import measures


@pytest.mark.parametrize(
    "name,amount,unit,expected",
    [
        # Container units are the whole point: the source ships a pre-portioned
        # dose and states no weight, so spoons are the only actionable measure.
        ("Ground Turmeric", 1, "sachet(s)", 3.5),
        ("Sri Lankan Curry Powder", 1, "pot(s)", 5.0),
        ("Ground Turmeric", 2, "sachet(s)", 7.0),
        # Low-density leaf herbs hold far less for the same fill, so they carry
        # a by_name override rather than the generic per-unit figure.
        ("Dried Oregano", 1, "sachet(s)", 2.0),
        # Nothing to translate: already metric, already a spoon, or not the kind
        # of thing anyone spoons out of a jar.
        ("Basmati Rice", 150, "grams", None),
        ("Olive Oil", 1, "tbsp", None),
        ("Coriander", 1, "bunch(es)", None),
        ("Red Onion", 1, "unit(s)", None),
        # Degenerate inputs, including the zero-amount lines the source emits
        # for ingredients it has swapped out of a recipe.
        ("Ground Turmeric", 0, "sachet(s)", None),
        ("Ground Turmeric", None, "sachet(s)", None),
        ("Ground Turmeric", 1, None, None),
    ],
)
def test_spoons_for(name, amount, unit, expected):
    assert measures.spoons_for(name, amount, unit) == expected


def test_spoons_scale_with_the_number_of_containers():
    one = measures.spoons_for("Ground Cumin", 1, "sachet(s)")
    three = measures.spoons_for("Ground Cumin", 3, "sachet(s)")
    assert three == pytest.approx(one * 3)


@pytest.mark.parametrize(
    "unit,estimated",
    [
        # The source said grams: the weight is its number, not ours.
        ("grams", False),
        ("milliliter(s)", False),
        # Everything else reached grams through a reference table.
        ("sachet(s)", True),
        ("unit(s)", True),
        ("bunch(es)", True),
        ("tbsp", True),
        (None, True),
    ],
)
def test_amount_g_is_estimated(unit, estimated):
    assert measures.amount_g_is_estimated(unit) is estimated


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Ground Cloves", measures.POTENCY_HIGH),
        ("Ground Cinnamon", measures.POTENCY_HIGH),
        ("Chilli Flakes", measures.POTENCY_HIGH),
        ("Saffron", measures.POTENCY_HIGH),
        ("Dried Oregano", measures.POTENCY_FORGIVING),
        ("Flat Leaf Parsley", measures.POTENCY_FORGIVING),
        ("British Chicken Breasts", measures.POTENCY_NORMAL),
        ("Basmati Rice", measures.POTENCY_NORMAL),
    ],
)
def test_potency_for(name, expected):
    assert measures.potency_for(name) == expected


@pytest.mark.parametrize("name", ["Garlic Clove", "Garlic Cloves"])
def test_garlic_is_not_the_clove_spice(name):
    """A substring match on "clove" would flag every garlic line in the library."""
    assert measures.potency_for(name) == measures.POTENCY_NORMAL


@pytest.mark.parametrize("name", ["Salad Potatoes", "Greek Style Salad Cheese"])
def test_salad_items_are_not_seasonings(name):
    """Keyword lists earn their keep only if they do not catch unrelated foods."""
    assert measures.potency_for(name) == measures.POTENCY_NORMAL
