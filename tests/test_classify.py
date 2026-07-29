"""Tests for the derived per-recipe signals."""
from __future__ import annotations

from app.classify import (
    diet_flags,
    effective_ratings,
    macros_suspect,
    protein_energy_ratio,
)


def test_effective_ratings_prefers_the_broader_lineage_sample():
    # The revision never ran long enough to be rated; the dish is well proven.
    assert effective_ratings(0, 0, 4.47, 1788) == (4.47, 1788)
    # Both populated: the lineage still spans more cooks than the revision.
    assert effective_ratings(3.44, 508, 4.36, 719) == (4.36, 719)


def test_effective_ratings_falls_back_to_the_revision():
    # No lineage figures at all (an older payload) -> use what the row has.
    assert effective_ratings(4.1, 300, None, None) == (4.1, 300)
    assert effective_ratings(4.1, 300, 0.0, 0) == (4.1, 300)
    # A lineage narrower than the revision itself is not a better sample.
    assert effective_ratings(4.1, 300, 3.0, 12) == (4.1, 300)


def test_effective_ratings_with_nothing_known():
    assert effective_ratings(None, None, None, None) == (None, None)


def test_protein_energy_ratio():
    assert protein_energy_ratio(35, 651) == 5.4
    assert protein_energy_ratio(0, 651) is None
    assert protein_energy_ratio(35, 0) is None
    assert protein_energy_ratio(35, None) is None


def test_macros_suspect_flags_inconsistent():
    # #2210: 4*78 + 4*39 + 9*38 = 810 vs stated 642 -> 26% over -> suspect.
    assert macros_suspect(78, 39, 38, 642) is True
    # #2274 and #902 reconcile.
    assert macros_suspect(78, 39, 37, 823) is False
    assert macros_suspect(44, 61, 39, 761) is False
    # Missing a macro -> cannot judge -> not suspect.
    assert macros_suspect(50, None, 20, 600) is False


def test_vegetarian_and_pescatarian():
    veg = diet_flags(["Halloumi", "Freekeh", "Onion", "Lemon"], [], 40, 600)
    assert veg["is_vegetarian"] is True
    assert veg["is_pescatarian"] is True  # veg dishes suit pescatarians too

    chicken = diet_flags(["Roasted Chicken Breast", "Halloumi"], [], 40, 800)
    assert chicken["is_vegetarian"] is False
    assert chicken["is_pescatarian"] is False

    fish = diet_flags(["Salmon Fillet", "Rice", "Broccoli"], ["Fish"], 60, 500)
    assert fish["is_vegetarian"] is False
    assert fish["is_pescatarian"] is True


def test_plant_based_override():
    # "Plant-Based Mince" contains 'mince' but the plant token cancels it.
    f = diet_flags(["Plant-Based Mince", "Tomato", "Onion"], [], 30, 400)
    assert f["is_vegetarian"] is True


def test_dairy_free_combines_allergen_and_ingredient():
    # Halloumi with no Milk allergen (a source gap) is still not dairy-free.
    assert diet_flags(["Halloumi", "Rice"], [], 40, 600)["is_dairy_free"] is False
    # Coconut milk is not dairy despite the word "milk".
    assert diet_flags(["Coconut Milk", "Rice"], [], 40, 600)["is_dairy_free"] is True
    # Explicit Milk allergen blocks it.
    assert diet_flags(["Butter", "Bread"], ["Milk"], 40, 600)["is_dairy_free"] is False


def test_gluten_free_and_butternut_false_friend():
    # Butternut squash must not trip the 'butter' dairy keyword.
    f = diet_flags(["Butternut Squash", "Rice", "Coconut Milk"], [], 40, 600)
    assert f["is_dairy_free"] is True
    assert f["is_gluten_free"] is True
    # Freekeh (wheat) with a gluten allergen is not gluten-free.
    assert diet_flags(["Freekeh", "Onion"], ["Cereals containing gluten"], 40, 600)[
        "is_gluten_free"
    ] is False


def test_low_carb_threshold():
    # carb energy fraction < 0.30 -> low carb.
    assert diet_flags(["Steak"], [], 20, 600)["is_low_carb"] is True  # 80/600=0.13
    assert diet_flags(["Pasta"], [], 80, 600)["is_low_carb"] is False  # 320/600=0.53
