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
        # Every figure here is grams / g_per_tsp, never stored independently.
        ("Ground Turmeric", 1, "sachet(s)", 1.0),
        ("Sri Lankan Curry Powder", 1, "pot(s)", 3.04),
        ("Ground Turmeric", 2, "sachet(s)", 2.0),
        # Low-density leaf herbs hold far less for the same fill, so they carry
        # a by_name entry rather than the generic per-unit figure.
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
    # Loose tolerance because each call rounds to 2dp independently, so scaling a
    # rounded figure and rounding a scaled one differ in the last digit.
    assert three == pytest.approx(one * 3, abs=0.02)


MEASURED = [
    # Read off delivered HelloFresh packaging. These are the only hard evidence in
    # the whole file and no later edit may drift off them.
    ("Tandoori Masala Mix", "sachet(s)", 5.0),
    ("Indonesian Style Spice Mix", "sachet(s)", 3.5),
]


@pytest.mark.parametrize("name,unit,grams", MEASURED)
def test_measured_sachet_masses_are_pinned(name, unit, grams):
    from app.canonicalize import to_grams

    assert to_grams(name, 1, unit) == (grams, "g")


@pytest.mark.parametrize("name,unit,_grams", MEASURED)
def test_measured_entries_are_marked_as_such(name, unit, _grams):
    from app.canonicalize import spice_dose

    assert spice_dose(name, unit)["source"] == "measured"


def test_spoon_range_brackets_the_typical_dose():
    for name, unit in [("Ground Cloves", "sachet(s)"), ("Ground Cumin", "sachet(s)"),
                       ("Tandoori Masala Mix", "sachet(s)")]:
        typical = measures.spoons_for(name, 1, unit)
        lo, hi = measures.spoon_range_for(name, 1, unit)
        assert lo <= typical <= hi, f"{name}: {typical} outside {lo}-{hi}"


def test_spoon_range_scales_and_is_absent_for_non_containers():
    lo, hi = measures.spoon_range_for("Ground Cumin", 2, "sachet(s)")
    one_lo, one_hi = measures.spoon_range_for("Ground Cumin", 1, "sachet(s)")
    assert (lo, hi) == pytest.approx((one_lo * 2, one_hi * 2), abs=0.02)
    assert measures.spoon_range_for("Basmati Rice", 150, "grams") is None
    assert measures.spoon_range_for("Red Onion", 1, "unit(s)") is None


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


def _all_dose_entries():
    from app.canonicalize import _spice_doses

    doses = _spice_doses()
    for name, entry in doses["by_name"].items():
        yield name, entry
    for unit, kinds in doses["defaults"].items():
        for kind, entry in kinds.items():
            yield f"default {unit}/{kind}", entry


def test_every_dose_implies_a_physically_possible_density():
    """A spoonful of dry seasoning weighs 1-6 g; anything else is a typo or a drift.

    This is the check that would have caught the tables disagreeing: ground cloves
    at half a teaspoon and eight grams implies 16 g/tsp, four times denser than
    any spice.
    """
    for name, entry in _all_dose_entries():
        g_per_tsp = entry["g_per_tsp"]
        assert 0.8 <= g_per_tsp <= 6.0, f"{name}: {g_per_tsp} g/tsp is not a real density"


def test_every_dose_typical_sits_inside_its_own_range():
    for name, entry in _all_dose_entries():
        typical = entry["grams"] / entry["g_per_tsp"]
        assert entry["tsp_min"] <= typical <= entry["tsp_max"], (
            f"{name}: {typical:.2f} tsp outside declared {entry['tsp_min']}-{entry['tsp_max']}"
        )


def test_keyword_matches_cannot_exceed_their_container():
    """The bug this prevents: "Chicken Stock Powder" weighing 160 g a sachet.

    `by_keyword` matches substrings and knows nothing about the container, so
    "chicken" made a stock sachet as heavy as a chicken breast.
    """
    from app.canonicalize import _reference, to_grams

    ceilings = _reference()["unit_ceiling"]
    for name in ("Chicken Stock Powder", "KNORR Chicken Stock", "Beef Stock Powder"):
        for unit, ceiling in ceilings.items():
            grams, _ = to_grams(name, 1, unit)
            if grams is not None:
                assert grams <= ceiling, f"{name} [{unit}] = {grams}g exceeds {ceiling}g"


def test_stock_is_not_priced_as_the_animal_it_names():
    from app.canonicalize import to_grams

    assert to_grams("Chicken Stock Powder", 1, "sachet(s)")[0] < 20
    # ...while the real thing is unaffected.
    assert to_grams("British Chicken Breasts", 1, "unit(s)")[0] == 160.0
