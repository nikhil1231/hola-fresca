"""Tests for the derived per-recipe signals."""
from __future__ import annotations

from app.classify import (
    course,
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


# --- course ----------------------------------------------------------------

# A plate with a base and a portion on it, so that the tag and title rules under
# test are what decide, not the fallback.
DINNER = [
    ("Basmati Rice", 150.0),
    ("British Chicken Breasts", 260.0),
    ("Onion", 110.0),
    ("Garlic Clove", 10.0),
    ("Coriander", 10.0),
]


def test_a_single_ingredient_recipe_is_a_bought_product():
    """Nothing to cook: houmous, a garlic baguette, a tub of chips. The source
    files some of these under veggie or nothing at all, so structure decides."""
    tub = [("Houmous", 300.0)]
    assert course(["grocery", "addon-veggie"], tub) == "product"
    assert course(["veggie"], tub) == "product"
    assert course([], tub) == "product"
    assert course(["lunch-readymeals"], tub) == "product"


def test_a_single_ingredient_dessert_is_still_a_dessert():
    assert course(["dessert-ready"], [("Gü Chocolate Mousse", 250.0)]) == "dessert"


def test_ready_meals_are_products_however_many_lines_they_list():
    assert course(["lunch-readymeals"], DINNER) == "product"


def test_desserts_are_recognised_even_when_they_are_real_cooking():
    assert course(["dessert-baking"], DINNER) == "dessert"
    assert course(["dessert-baking", "eggs-not-included"], DINNER) == "dessert"


def test_side_tagged_dishes_are_sides_however_big_they_are():
    """The source uses these on nine-ingredient sharing platters as readily as
    on a tub of slaw, so there is no size at which the tag stops meaning side."""
    assert course(["sides-salad"], DINNER) == "side"
    assert course(["sides-bites"], DINNER) == "side"
    assert course(["grocery-bakery"], DINNER) == "side"


def test_lunch_and_addon_tags_are_not_course_markers():
    """These sound like accompaniments and are not: a Green Goddess Rump Steak
    Salad and a Chicken and Chorizo Paella carry them."""
    assert course(["lunch-salad"], DINNER) == "main"
    assert course(["lunch-pasta"], DINNER) == "main"
    assert course(["addon-veggie"], DINNER) == "main"
    assert course(["addon-highprotein", "high-protein"], DINNER) == "main"


def test_breakfast_is_its_own_course():
    assert course(["breakfast-kits"], DINNER) == "breakfast"
    assert course(["breakfast-juices", "addon-veggie"], DINNER) == "breakfast"
    assert course(
        [], DINNER, name="Zesty Cream Cheese & Avocado Breakfast Ciabatta"
    ) == "breakfast"
    assert course([], DINNER, name="Super Green Smoothie Kit") == "breakfast"


def test_a_course_printed_in_the_title_block_is_believed():
    """#30948: nine ingredients and 40 g of chicken, and the source says Starter."""
    assert course(
        [],
        DINNER,
        name="Bestselling Chicken Peanut Satay Style Skewers",
        headline="Starter | with a Sticky Peanut Dipping Sauce and Lime",
    ) == "side"
    assert course([], DINNER, name="BLT Side Salad") == "side"
    assert course(
        [], DINNER, headline="Sharing Dish | with Cheddar Cheese"
    ) == "side"


def test_a_course_word_describing_the_accompaniment_is_not_the_course():
    """Eighty real dinners come "with a Rocket Side Salad"."""
    assert course(
        [],
        DINNER,
        name="Honeyed Chorizo & Cheddar Pan-Fried Panini",
        headline="Serves 2 | with a Rocket Side Salad",
    ) == "main"
    assert course(
        [], DINNER, name="Veggie Enchiladas with a Side Helping of Mexican Trivia"
    ) == "main"


def test_a_plate_with_neither_base_nor_portion_is_a_side():
    """#20405 Garlicky Greens: cavolo nero and a 45 g-a-head scattering of
    lardons. #28356 is six ingredients of dressed kale."""
    assert course(
        [],
        [
            ("Garlic Clove", 10.0),
            ("Hazelnuts", 25.0),
            ("British Smoked Bacon Lardons", 90.0),
            ("Chopped Cavolo Nero", 200.0),
            ("Water", 30.0),
        ],
        name="Garlicky Greens",
        headline="with Bacon and Hazelnuts",
        servings=2,
    ) == "side"
    assert course(
        [],
        [
            ("Sesame Oil", 15.0),
            ("Soy Sauce", 30.0),
            ("Honey", 20.0),
            ("Chopped Kale", 200.0),
            ("Salted Peanuts", 25.0),
            ("Lime", 65.0),
        ],
        name="Stir fried Asian Style Kale Salad",
        servings=2,
    ) == "side"


def test_an_unquantified_base_still_counts_as_one():
    """The source leaves whole recipes at 0 g. A Chicken Biryani whose rice and
    thighs both read zero is still a biryani, not a side."""
    assert course(
        [],
        [
            ("Basmati Rice", 0.0),
            ("Diced British Chicken Thigh", 0.0),
            ("Onion", 0.0),
            ("Sri Lankan Curry Powder", None),
        ],
        name="Chicken Biryani",
        headline="with Herby Chilli Yoghurt",
        servings=2,
    ) == "main"


def test_an_ordinary_dinner_is_a_main():
    assert course(["rapid", "bestseller", "seo"], DINNER) == "main"
    assert course(None, DINNER) == "main"
    # A steak dinner has no starch on the plate; the portion carries it.
    assert course(
        None,
        [
            ("Sirloin Steak", 300.0),
            ("Tenderstem Broccoli", 200.0),
            ("Garlic Clove", 10.0),
            ("Creme Fraiche", 75.0),
        ],
        name="Surf 'n' Turf Steak",
        headline="with a Creamy Garlic Peppercorn Sauce",
        servings=2,
    ) == "main"
