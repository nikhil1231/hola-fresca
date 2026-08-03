"""Protein swaps and scaling: the arithmetic, the catalogue, and the plumbing.

Most of this is pure — a modifier is a function of a recipe and a reference
table — so it is tested without a database. The tail runs the same modifiers
through the real index so the two paths through the basket engine cannot drift.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import protein as P
from app.api.deps import get_planner_csv_path, get_session, get_session_factory
from app.db.models import Recipe, RecipeIngredient, RecipeStep
from app.mapping import service
from app.mapping.candidates import gather_candidates
from app.planner import basket as B
from app.planner.index import load_index, modified_needs
from main import app
from tests.conftest import seed_candidates
from tests.test_planner_basket import write_freq_csv

CHICKEN = "name:british chicken breasts"
MINCE = "name:british beef mince"
STOCK = "name:chicken stock paste"
VEG_STOCK = "name:vegetable stock paste"
TOFU = "name:firm tofu"

# A plausible chicken dinner: 320 g of breast across two portions.
MACROS = P.Macros(kcal=650, protein_g=45, fat_g=20, carbs_g=60)


def line(key=CHICKEN, name="British Chicken Breasts", grams=320.0, units=None):
    return P.find_protein_line([(key, name, grams, units)])


def resolve(modifier, *, protein_line=None, macros=MACROS, base_yield=2):
    return P.resolve(
        protein_line or line(), modifier, base_yield=base_yield, recipe_macros=macros
    )


# --------------------------------------------------------------------------
# Finding the protein
# --------------------------------------------------------------------------

def test_the_heaviest_protein_wins_not_the_first():
    """Chicken and chorizo paella is a chicken dish; the chorizo is seasoning."""
    found = P.find_protein_line([
        ("name:diced chorizo", "Diced Chorizo", 80.0, None),
        (CHICKEN, "British Chicken Breasts", 320.0, 2.0),
        ("name:basmati rice", "Basmati Rice", 300.0, None),
    ])
    assert found is not None
    assert found.key == CHICKEN


def test_a_garnish_of_meat_is_not_the_dishs_protein():
    """15 g of vegan nduja is a flavouring. Nothing here is swappable."""
    assert P.find_protein_line([("name:vegan nduja", "Vegan 'Nduja", 15.0, None)]) is None


def test_a_vegetable_dish_has_no_protein_line():
    assert P.find_protein_line([("name:mixed beans", "Mixed Beans", 240.0, None)]) is None


# --------------------------------------------------------------------------
# The type-by-form catalogue
# --------------------------------------------------------------------------

def test_a_mince_recipe_is_never_offered_a_whole_fillet():
    """Salmon and basa are sold as fillets; a chilli con carne cannot use one."""
    offered = {t.id for t in P.targets_for_form("pieces")}
    assert "salmon" not in offered and "basa" not in offered
    assert {"beef", "pork", "lamb", "chicken_breast", "tofu", "halloumi"} <= offered


def test_a_whole_piece_recipe_is_never_offered_mince():
    offered = {t.id for t in P.targets_for_form("whole")}
    assert "beef" not in offered and "pork" not in offered and "lamb" not in offered
    assert {"chicken_breast", "chicken_thigh", "salmon", "basa", "tofu"} <= offered


def test_chicken_arrives_diced_in_a_mince_recipe_and_whole_otherwise():
    """One target, two products — the form is what decides which is bought."""
    chicken = P.target("chicken_breast")
    assert chicken.key_for("pieces") == "name:diced british chicken breast"
    assert chicken.key_for("whole") == CHICKEN


def test_a_target_that_does_not_suit_the_form_is_refused_not_forced():
    result = resolve(P.ProteinModifier(swap_to="beef"))
    assert not result.swapped
    assert result.factor == 1.0
    assert any("not sold in a form" in w for w in result.warnings)


# --------------------------------------------------------------------------
# Swapping and scaling
# --------------------------------------------------------------------------

def test_a_swap_keeps_the_weight_and_moves_the_macros():
    """Equal weight is the rule: the dish stays the same size, not the same macros."""
    result = resolve(P.ProteinModifier(swap_to="tofu"))
    assert result.target_key == TOFU
    assert result.grams_after == pytest.approx(result.grams_before)
    # 160 g of chicken a portion at 24 g/100 g is 38.4 g of the stated 45 g; tofu
    # puts back 22.4 g.
    assert result.macros_after.protein_g == pytest.approx(45 - 38.4 + 22.4, abs=0.05)


def test_scaling_leaves_every_other_ingredient_alone():
    """Doubling the protein doubles the protein, and the spice mix is untouched."""
    result = resolve(P.ProteinModifier(scale=2.0))
    assert result.grams_after == pytest.approx(640.0)
    assert not result.swapped
    assert result.residual.kcal == pytest.approx(650 - 1.6 * 106, abs=0.5)


def test_a_protein_target_is_solved_exactly_not_approached():
    result = resolve(P.ProteinModifier(target_mode="protein_g", target_value=50))
    assert result.macros_after.protein_g == pytest.approx(50.0, abs=0.05)


def test_a_calorie_target_is_solved_against_the_rest_of_the_dish():
    result = resolve(P.ProteinModifier(target_mode="energy_kcal", target_value=700))
    assert result.macros_after.kcal == pytest.approx(700, abs=1)


def test_a_target_and_a_swap_solve_against_the_new_protein():
    """50 g of protein from tofu is a lot more tofu than it is chicken."""
    chicken = resolve(P.ProteinModifier(target_mode="protein_g", target_value=50))
    tofu = resolve(P.ProteinModifier(swap_to="tofu", target_mode="protein_g", target_value=50))
    assert tofu.macros_after.protein_g == pytest.approx(50.0, abs=0.05)
    assert tofu.grams_after > chicken.grams_after * 1.5


def test_an_impossible_target_is_capped_and_says_so():
    """No amount of chicken makes a 2000 kcal portion of this dish."""
    result = resolve(P.ProteinModifier(target_mode="energy_kcal", target_value=2000))
    assert result.factor == P.MAX_FACTOR
    assert any("Capped" in w for w in result.warnings)


def test_a_target_the_rest_of_the_dish_already_meets_falls_to_the_floor():
    """Asking for 20 g of protein when the sides alone bring 30 g cannot be done
    by cutting the meat, and pretending otherwise would return a negative weight."""
    result = resolve(
        P.ProteinModifier(target_mode="protein_g", target_value=5),
        macros=P.Macros(kcal=650, protein_g=45, fat_g=20, carbs_g=60),
    )
    assert result.factor == P.MIN_FACTOR
    assert any("already provides" in w for w in result.warnings)


def test_macros_that_cannot_account_for_their_own_protein_are_flagged():
    """A published figure smaller than the protein line implies is a data problem,
    not a reason to return a negative residual."""
    result = resolve(P.ProteinModifier(scale=2.0), macros=P.Macros(kcal=200, protein_g=10))
    assert result.residual.protein_g == 0
    assert any("do not fully account" in w for w in result.warnings)


def test_a_swap_to_what_is_already_there_changes_nothing():
    result = resolve(P.ProteinModifier(swap_to="chicken_breast"))
    assert not result.changed


# --------------------------------------------------------------------------
# Quantities
# --------------------------------------------------------------------------

def test_a_counted_protein_lands_on_a_number_of_pieces():
    """You can cook half a fillet. You cannot cook 0.37 of one."""
    grams, units = P.swapped_quantity(650.0, unit_kind="count", each_to_grams=130.0)
    assert units == 5.0
    assert grams == pytest.approx(650.0)

    grams, units = P.swapped_quantity(300.0, unit_kind="count", each_to_grams=174.0)
    assert units == 1.5
    assert grams == pytest.approx(261.0)


def test_a_weighed_protein_keeps_its_exact_weight():
    assert P.swapped_quantity(347.0, unit_kind="mass", each_to_grams=None) == (347.0, None)


# --------------------------------------------------------------------------
# Companions and text
# --------------------------------------------------------------------------

def test_going_meat_free_takes_the_meat_stock_with_it():
    """A "vegetarian" swap that leaves chicken stock in the pan is not one."""
    result = resolve(P.ProteinModifier(swap_to="tofu"))
    assert P.companion_swaps([STOCK, "name:onion"], result) == {STOCK: VEG_STOCK}


def test_swapping_one_meat_for_another_leaves_the_stock_alone():
    result = resolve(P.ProteinModifier(swap_to="chicken_thigh"))
    assert P.companion_swaps([STOCK], result) == {}


def test_scaling_alone_never_touches_a_companion():
    assert P.companion_swaps([STOCK], resolve(P.ProteinModifier(scale=2.0))) == {}


def test_a_step_renames_the_protein_but_not_the_stock():
    """"Chicken stock" survives a swap to tofu; the chicken in the pan does not."""
    result = resolve(P.ProteinModifier(swap_to="tofu"))
    rewritten = P.rewrite_text(
        "Fry the British Chicken Breasts for 5 mins, then stir in the chicken stock paste "
        "and return the chicken to the pan.",
        result,
    )
    assert "chicken stock paste" in rewritten
    assert "Chicken Breasts" not in rewritten
    assert rewritten.lower().count("tofu") == 2


def test_a_step_is_left_verbatim_when_nothing_was_swapped():
    text = "Fry the chicken for 5 mins."
    assert P.rewrite_text(text, resolve(P.ProteinModifier(scale=2.0))) == text


def test_a_line_named_after_the_protein_is_relabelled():
    result = resolve(P.ProteinModifier(swap_to="tofu"))
    assert P.rename_companion("Oil for the Chicken", result) == "Oil for the Tofu"
    assert P.rename_companion("Olive Oil", result) == "Olive Oil"


# --------------------------------------------------------------------------
# Through the real index and basket
# --------------------------------------------------------------------------

def _seed(session, key, name, sku, price, pack_value):
    seed_candidates(
        session, key, name,
        [{"sku": sku, "name": f"{name} {pack_value}g", "price": price,
          "pack_value": pack_value, "pack_unit": "g"}],
    )
    service.save_decision(
        session, gather_candidates(session, key),
        service.DecisionInput(
            status="approved", accepted=[service.AcceptedInput(sku=sku, rank=1)]
        ),
    )


def _seed_world(factory, tmp_path):
    """A chicken dinner, a mince dinner, and the shelf they are bought from."""
    rows = [
        (CHICKEN, "sid-chicken", "British Chicken Breasts"),
        (MINCE, "sid-mince", "British Beef Mince"),
        (TOFU, "sid-tofu", "Firm Tofu"),
        (STOCK, "sid-stock", "Chicken Stock Paste"),
        (VEG_STOCK, "sid-vegstock", "Vegetable Stock Paste"),
    ]
    csv_path = write_freq_csv(tmp_path / "freq.csv", rows)
    with factory() as s:
        _seed(s, CHICKEN, "British Chicken Breasts", "chicken", 4.00, 320)
        _seed(s, MINCE, "British Beef Mince", "mince", 3.50, 500)
        _seed(s, TOFU, "Firm Tofu", "tofu", 2.00, 280)
        _seed(s, STOCK, "Chicken Stock Paste", "stock", 2.50, 100)
        _seed(s, VEG_STOCK, "Vegetable Stock Paste", "vegstock", 2.20, 100)
        chicken = Recipe(
            source="hellofresh", source_id="chicken-dinner", url="", name="Chicken Dinner",
            curated=1, base_yield=2, energy_kcal=650, protein_g=45, fat_g=20, carbs_g=60,
            ingredients=[
                RecipeIngredient(name="British Chicken Breasts", source_ingredient_id="sid-chicken",
                                 amount=320, unit="grams", amount_g=320, position=1),
                RecipeIngredient(name="Chicken Stock Paste", source_ingredient_id="sid-stock",
                                 amount=20, unit="grams", amount_g=20, position=2),
            ],
            steps=[
                RecipeStep(index=1, instructions_text=(
                    "Fry the British Chicken Breasts for 5 mins, then stir in the "
                    "chicken stock paste."
                )),
            ],
        )
        # Already uses tofu alongside its mince, so a swap to tofu has to merge
        # rather than ask for it twice.
        both = Recipe(
            source="hellofresh", source_id="mince-and-tofu", url="", name="Mince and Tofu",
            curated=1, base_yield=2, energy_kcal=700, protein_g=50, fat_g=25, carbs_g=55,
            ingredients=[
                RecipeIngredient(name="British Beef Mince", source_ingredient_id="sid-mince",
                                 amount=300, unit="grams", amount_g=300),
                RecipeIngredient(name="Firm Tofu", source_ingredient_id="sid-tofu",
                                 amount=100, unit="grams", amount_g=100),
            ],
        )
        s.add_all([chicken, both])
        s.commit()
        ids = {"chicken": chicken.id, "both": both.id}
    return csv_path, ids


@pytest.fixture
def protein_index(factory, tmp_path):
    csv_path, ids = _seed_world(factory, tmp_path)
    return load_index(factory, csv_path=csv_path), ids


@pytest.fixture
def protein_client(factory, tmp_path):
    csv_path, ids = _seed_world(factory, tmp_path)

    def _override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_planner_csv_path] = lambda: csv_path
    yield TestClient(app), ids
    app.dependency_overrides.clear()


def test_the_index_finds_each_recipes_protein(protein_index):
    index, ids = protein_index
    assert index.recipes[ids["chicken"]].protein.key == CHICKEN
    assert index.recipes[ids["both"]].protein.key == MINCE


def test_a_swap_buys_the_new_protein_and_stops_buying_the_old(protein_index):
    index, ids = protein_index
    selections = [B.Selection(ids["chicken"], 2, P.ProteinModifier(swap_to="tofu"))]
    lines = {line.key: line for line in B.build_basket(index, selections).lines}
    assert TOFU in lines
    assert CHICKEN not in lines
    assert lines[TOFU].need_g == pytest.approx(320.0)
    # The chicken stock went with it, or the dish is not meat-free at all.
    assert VEG_STOCK in lines and STOCK not in lines


def test_scaling_multiplies_only_the_protein(protein_index):
    index, ids = protein_index
    selections = [B.Selection(ids["chicken"], 2, P.ProteinModifier(scale=2.0))]
    lines = {line.key: line for line in B.build_basket(index, selections).lines}
    assert lines[CHICKEN].need_g == pytest.approx(640.0)
    assert lines[STOCK].need_g == pytest.approx(20.0)


def test_portions_and_the_modifier_multiply_rather_than_replace(protein_index):
    """A modifier is a statement about a portion, so four portions of double
    protein is four times two, not one or the other."""
    index, ids = protein_index
    selections = [B.Selection(ids["chicken"], 4, P.ProteinModifier(scale=2.0))]
    lines = {line.key: line for line in B.build_basket(index, selections).lines}
    assert lines[CHICKEN].need_g == pytest.approx(1280.0)


def test_a_swap_onto_an_ingredient_already_in_the_recipe_is_bought_once(protein_index):
    index, ids = protein_index
    selections = [B.Selection(ids["both"], 2, P.ProteinModifier(swap_to="tofu"))]
    basket = B.build_basket(index, selections)
    tofu = [line for line in basket.lines if line.key == TOFU]
    assert len(tofu) == 1
    assert tofu[0].need_g == pytest.approx(400.0)


def test_an_unmodified_selection_gets_the_recipes_own_needs(protein_index):
    index, ids = protein_index
    recipe = index.recipes[ids["chicken"]]
    assert modified_needs(index, recipe, None) is recipe.needs
    assert modified_needs(index, recipe, P.ProteinModifier()) is recipe.needs


def test_score_basket_agrees_with_build_basket_under_a_modifier(protein_index):
    """The ranking's lean path has to price a modified week the same way the
    basket page does, or the two start disagreeing about the same week."""
    index, ids = protein_index
    modifiers = [
        None,
        P.ProteinModifier(swap_to="tofu"),
        P.ProteinModifier(scale=1.5),
        P.ProteinModifier(target_mode="protein_g", target_value=50),
        P.ProteinModifier(swap_to="chicken_thigh", scale=2.0),
    ]
    for modifier in modifiers:
        selections = [
            B.Selection(ids["chicken"], 4, modifier),
            B.Selection(ids["both"], 2),
        ]
        built = B.build_basket(index, selections)
        scored = B.score_basket(index, selections)
        assert scored.score == pytest.approx(built.score)
        assert scored.cost == pytest.approx(built.cost)
        assert scored.consumed_cost == pytest.approx(built.consumed_cost)
        assert scored.gap_count == B.basket_gap_count(built)


# --------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------

def test_the_detail_page_is_told_what_can_replace_the_protein(protein_client):
    client, ids = protein_client
    profile = client.get(f"/api/recipes/{ids['chicken']}").json()["protein"]
    assert profile["name"] == "British Chicken Breasts"
    assert profile["form"] == "whole"
    assert profile["grams"] == 320
    offered = {t["id"] for t in profile["targets"]}
    # Only what this recipe can be shopped for: tofu is stocked here, halloumi
    # is not mapped at all, and mince does not suit a whole-piece dish.
    assert "tofu" in offered
    assert "halloumi" not in offered
    assert "beef" not in offered


def test_a_mince_dish_refuses_a_fillet_swap_rather_than_forcing_it(protein_client):
    """The API is not the UI's honour system: an unsuitable form is refused here too."""
    client, ids = protein_client
    assert client.get(f"/api/recipes/{ids['both']}").json()["protein"]["type"] == "beef"
    body = client.post(
        f"/api/recipes/{ids['both']}/protein/preview", json={"swap_to": "salmon"}
    ).json()
    assert body["swapped"] is False
    assert body["warnings"]


def test_the_preview_returns_the_modified_recipe_not_a_diff(protein_client):
    client, ids = protein_client
    body = client.post(
        f"/api/recipes/{ids['chicken']}/protein/preview", json={"swap_to": "tofu"}
    ).json()

    assert body["swapped"] is True
    assert body["protein_name_after"] == "Firm Tofu"
    names = [i["name"] for i in body["ingredients"]]
    assert names == ["Firm Tofu", "Vegetable Stock Paste"]
    assert body["ingredients"][0]["ingredient_key"] == TOFU
    assert "tofu" in body["steps"][0]["text"].lower()
    assert "chicken stock paste" in body["steps"][0]["text"]
    assert "Now vegetarian" in body["diet_changes"]
    assert body["cook_note"]


def test_the_preview_leaves_the_stored_recipe_alone(protein_client):
    """A modifier is a hypothetical. The library must read the same afterwards."""
    client, ids = protein_client
    before = client.get(f"/api/recipes/{ids['chicken']}").json()
    client.post(f"/api/recipes/{ids['chicken']}/protein/preview", json={"scale": 2.0})
    after = client.get(f"/api/recipes/{ids['chicken']}").json()
    assert before == after


def test_the_basket_prices_the_modifier_on_the_selection(protein_client):
    client, ids = protein_client
    plain = client.post(
        "/api/planner/basket",
        json={"selections": [{"recipe_id": ids["chicken"], "portions": 2}]},
    ).json()
    swapped = client.post(
        "/api/planner/basket",
        json={
            "selections": [
                {"recipe_id": ids["chicken"], "portions": 2, "protein": {"swap_to": "tofu"}}
            ]
        },
    ).json()

    assert {line["name"] for line in plain["lines"]} == {
        "British Chicken Breasts",
        "Chicken Stock Paste",
    }
    assert {line["name"] for line in swapped["lines"]} == {"Firm Tofu", "Vegetable Stock Paste"}
    assert swapped["cost"] < plain["cost"]
