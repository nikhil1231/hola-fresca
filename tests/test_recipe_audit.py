"""Recipe macro audit: arithmetic first, a model only when it is the only option.

The behaviour worth protecting is restraint — the pass must not "correct" numbers
it cannot justify, because a confident wrong figure is worse than the original.
"""
from __future__ import annotations

import pytest

from app import audit
from app.db.models import Recipe, RecipeEdit, RecipeIngredient


def make_recipe(session, *, ingredients=None, **fields):
    defaults = dict(
        source="hellofresh", source_id="x", url="u", name="R", curated=1, base_yield=2,
    )
    recipe = Recipe(**{**defaults, **fields})
    recipe.ingredients = [
        RecipeIngredient(name=n, amount=g, unit="grams", amount_g=g)
        for n, g in (ingredients or [])
    ]
    session.add(recipe)
    session.commit()
    return recipe


# --------------------------------------------------------------------------
# Arithmetic
# --------------------------------------------------------------------------

def test_corrects_energy_that_the_macros_contradict(factory):
    with factory() as s:
        # 4*30 + 4*60 + 9*20 = 540 kcal, not the 900 claimed.
        r = make_recipe(s, energy_kcal=900, protein_g=30, carbs_g=60, fat_g=20)
        result = audit.audit_recipe(s, r.id, completer=None)
        assert result.verdict == "corrected"
        assert result.used_llm is False
        assert s.get(Recipe, r.id).energy_kcal == pytest.approx(540)


def test_leaves_macros_that_already_reconcile(factory):
    with factory() as s:
        r = make_recipe(s, energy_kcal=540, protein_g=30, carbs_g=60, fat_g=20)
        result = audit.audit_recipe(s, r.id, completer=None)
        assert result.verdict == "ok"
        assert result.findings == []
        assert s.get(Recipe, r.id).energy_kcal == 540


def test_solves_the_one_missing_macro(factory):
    with factory() as s:
        # 600 kcal, with 40g carbs (160) and 20g fat (180) leaving 260 for protein.
        r = make_recipe(s, energy_kcal=600, protein_g=None, carbs_g=40, fat_g=20)
        audit.audit_recipe(s, r.id, completer=None)
        assert s.get(Recipe, r.id).protein_g == pytest.approx(65.0)


def test_will_not_rewrite_energy_into_an_impossible_meal(factory):
    """A pasta main is not 217 kcal: here a *macro* is wrong, not the energy.

    The arithmetic cannot say which, so it must decline rather than confidently
    make the recipe worse.
    """
    with factory() as s:
        r = make_recipe(s, energy_kcal=497, protein_g=26, carbs_g=26, fat_g=1)
        assert audit.check_macro_arithmetic(r) == []
        assert any("one of the macros is wrong" in c for c in audit.check_plausibility(r))

        result = audit.audit_recipe(s, r.id, completer=None)
        assert result.verdict == "inconclusive"
        assert s.get(Recipe, r.id).energy_kcal == 497  # untouched


def test_flags_an_implausibly_large_serving(factory):
    with factory() as s:
        r = make_recipe(s, energy_kcal=2100, protein_g=150, carbs_g=100, fat_g=120)
        assert any("too much for one serving" in c for c in audit.check_plausibility(r))


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

def constant_completer(**per_100g):
    """A stub model returning the same reference values for every ingredient."""

    def complete(system, user, schema):
        names = [
            line[2:].split(" (")[0]
            for line in user.splitlines()
            if line.startswith("- ")
        ]
        return {"ingredients": [{"name": n, **per_100g} for n in names]}

    return complete


def test_recomputes_macros_from_ingredient_composition(factory):
    """The model supplies per-100g reference values; the sums stay in Python."""
    with factory() as s:
        # 2000 g of ingredients at 100 kcal/100g = 2000 kcal, over 2 servings = 1000.
        r = make_recipe(
            s,
            energy_kcal=2100, protein_g=150, carbs_g=100, fat_g=120,
            ingredients=[("Chicken", 1000), ("Potatoes", 1000)],
        )
        completer = constant_completer(
            kcal_per_100g=100, protein_per_100g=10, fat_per_100g=5, carbs_per_100g=8
        )
        result = audit.audit_recipe(s, r.id, completer=completer, model="cheap-model")
        assert result.used_llm is True
        assert "ingredient composition" in result.checked

        after = s.get(Recipe, r.id)
        assert after.energy_kcal == pytest.approx(1000)
        assert after.protein_g == pytest.approx(100)
        assert {e.source for e in after.edits} == {"llm"}
        assert {e.model for e in after.edits} == {"cheap-model"}


def test_ignores_a_composition_answer_covering_too_little_of_the_recipe(factory):
    """Half the ingredients unmatched means the total is not worth trusting."""
    with factory() as s:
        r = make_recipe(
            s,
            energy_kcal=2100, protein_g=150, carbs_g=100, fat_g=120,
            ingredients=[("Chicken", 1000), ("Potatoes", 500), ("Leek", 200), ("Butter", 50)],
        )

        def stingy(system, user, schema):
            return {"ingredients": [{
                "name": "Chicken", "kcal_per_100g": 100, "protein_per_100g": 10,
                "fat_per_100g": 5, "carbs_per_100g": 8,
            }]}

        result = audit.audit_recipe(s, r.id, completer=stingy)
        assert result.findings == []
        assert s.get(Recipe, r.id).energy_kcal == 2100


def test_no_model_available_is_inconclusive_not_a_failure(factory):
    with factory() as s:
        r = make_recipe(s, energy_kcal=2100, protein_g=150, carbs_g=100, fat_g=120)
        result = audit.audit_recipe(s, r.id, completer=None)
        assert result.verdict == "inconclusive"
        assert result.findings == []


def test_skips_a_correction_too_small_to_matter(factory):
    with factory() as s:
        r = make_recipe(
            s, energy_kcal=1000, protein_g=100, carbs_g=80, fat_g=40,
            ingredients=[("Chicken", 1000), ("Potatoes", 1000)],
        )
        # Implies exactly 1010 kcal per serving: a 1% change, not worth recording.
        completer = constant_completer(
            kcal_per_100g=101, protein_per_100g=10, fat_per_100g=2, carbs_per_100g=4
        )
        findings = audit.check_against_composition(r, completer)
        applied = audit.apply_findings(s, r, [f for f in findings if f.field == "energy_kcal"])
        assert applied == []


# --------------------------------------------------------------------------
# Provenance and revert
# --------------------------------------------------------------------------

def test_records_what_each_number_used_to_be(factory):
    with factory() as s:
        r = make_recipe(s, energy_kcal=900, protein_g=30, carbs_g=60, fat_g=20)
        audit.audit_recipe(s, r.id, completer=None)
        edits = s.query(RecipeEdit).filter_by(recipe_id=r.id).all()
        assert [(e.field, e.old_value, e.new_value, e.status) for e in edits] == [
            ("energy_kcal", 900.0, 540.0, "applied")
        ]
        assert edits[0].reason and "implies" in edits[0].reason


def test_revert_restores_the_original_source_value(factory):
    with factory() as s:
        r = make_recipe(s, energy_kcal=900, protein_g=30, carbs_g=60, fat_g=20)
        audit.audit_recipe(s, r.id, completer=None)
        assert s.get(Recipe, r.id).energy_kcal == pytest.approx(540)

        assert audit.revert_recipe(s, r.id) == 1
        after = s.get(Recipe, r.id)
        assert after.energy_kcal == 900
        assert all(e.status == "reverted" for e in after.edits)


def test_revert_goes_back_past_several_corrections(factory):
    """However many times a number was corrected, revert restores the source one."""
    with factory() as s:
        r = make_recipe(
            s, energy_kcal=900, protein_g=30, carbs_g=60, fat_g=20,
            ingredients=[("Chicken", 1000)],
        )
        audit.audit_recipe(s, r.id, completer=None)  # 900 -> 540 by arithmetic
        second = audit.Finding(
            field="energy_kcal", old_value=540.0, new_value=700.0, reason="second pass"
        )
        audit.apply_findings(s, s.get(Recipe, r.id), [second])
        s.commit()
        assert s.get(Recipe, r.id).energy_kcal == 700

        audit.revert_recipe(s, r.id)
        assert s.get(Recipe, r.id).energy_kcal == 900


def test_audit_keeps_the_derived_signals_consistent(factory):
    with factory() as s:
        r = make_recipe(s, energy_kcal=900, protein_g=30, carbs_g=60, fat_g=20, macros_suspect=1)
        audit.audit_recipe(s, r.id, completer=None)
        after = s.get(Recipe, r.id)
        # Now that energy matches the macros, it is no longer suspect.
        assert after.macros_suspect == 0
        assert after.protein_energy_ratio == pytest.approx(30 / 540 * 100, abs=0.1)


def test_audit_declining_to_guess_beats_a_confident_wrong_number(factory):
    """The two paths together: arithmetic abstains, composition finds the culprit.

    Trusting the macros here would have rewritten a 497 kcal pasta dish to 217.
    The real error is the carbohydrate figure, and only the ingredients show that.
    """
    with factory() as s:
        r = make_recipe(
            s,
            energy_kcal=497, protein_g=26, carbs_g=26, fat_g=1,
            base_yield=4,
            ingredients=[("Linguine", 720), ("King Prawns", 480), ("Chopped Tomatoes", 780)],
        )
        completer = constant_completer(
            kcal_per_100g=120, protein_per_100g=6, fat_per_100g=1, carbs_per_100g=20
        )
        result = audit.audit_recipe(s, r.id, completer=completer)

        corrected = {f.field for f in result.findings}
        assert "carbs_g" in corrected
        assert "energy_kcal" not in corrected  # the number that was actually right
        assert s.get(Recipe, r.id).energy_kcal == 497


def make_ingredient(name, amount, unit, amount_g):
    return RecipeIngredient(name=name, amount=amount, unit=unit, amount_g=amount_g)


def seed_corpus_norm(session, name, grams, *, lines=25):
    """Give an ingredient an established weight, so a placeholder stands out.

    The norm comes from the corpus, so the supporting lines need a recipe of their
    own rather than being orphans.
    """
    carrier = Recipe(source="hellofresh", source_id=f"norm-{name}", url="u", name=f"norm {name}")
    carrier.ingredients = [
        make_ingredient(name, grams, "grams", grams) for _ in range(lines)
    ]
    session.add(carrier)
    session.commit()


def test_reports_broken_quantities_even_when_the_macros_pass(factory):
    """Macros cross-checking against each other says nothing about the quantities.

    Real case: "Roasted Salmon" reconciles to 1% but records no weight for the
    potatoes and 1 g of green beans — which is what the basket is built from.
    """
    with factory() as s:
        r = make_recipe(s, energy_kcal=362, protein_g=37, carbs_g=39, fat_g=6)
        r.ingredients = [
            make_ingredient("Salmon Fillet", 2, "unit(s)", 260),
            make_ingredient("Green Beans", 1, "grams", 1),
            make_ingredient("New Potatoes", 1, "unit(s)", None),
        ]
        s.commit()
        # Establish the corpus norm the 1 g line is judged against.
        seed_corpus_norm(s, "Green Beans", 150)

        result = audit.audit_recipe(s, r.id, completer=None)
        assert result.verdict == "ok"  # the macros really are fine
        assert any("New Potatoes" in g for g in result.ingredient_gaps)
        assert any("Green Beans" in g and "typically 150" in g for g in result.ingredient_gaps)


def test_a_small_spoon_measurement_is_not_a_broken_quantity(factory):
    """"Sugar for the Sauce, ½ tsp" really is 2.5 g. Flagging it is crying wolf."""
    with factory() as s:
        r = make_recipe(s, energy_kcal=540, protein_g=30, carbs_g=60, fat_g=20)
        r.ingredients = [
            make_ingredient("Sugar for the Sauce", 0.5, "tsp", 2.5),
            make_ingredient("Olive Oil", 1, "tbsp", 15),
            make_ingredient("Chicken", 500, "grams", 500),
        ]
        s.commit()
        assert audit.composition_blockers(r, audit.typical_weights(s, r)) == []


def test_a_genuinely_small_gram_amount_is_left_alone(factory):
    """5 g of sesame seeds is real: the norm for sesame seeds is itself small."""
    with factory() as s:
        r = make_recipe(s, energy_kcal=540, protein_g=30, carbs_g=60, fat_g=20)
        r.ingredients = [make_ingredient("Sesame Seeds", 5, "grams", 5)]
        s.commit()
        seed_corpus_norm(s, "Sesame Seeds", 12)
        assert audit.composition_blockers(r, audit.typical_weights(s, r)) == []


def test_an_ingredient_with_no_established_norm_is_not_judged(factory):
    with factory() as s:
        r = make_recipe(s, energy_kcal=540, protein_g=30, carbs_g=60, fat_g=20)
        r.ingredients = [make_ingredient("Mystery Powder", 1, "grams", 1)]
        s.commit()
        assert audit.composition_blockers(r, audit.typical_weights(s, r)) == []


def test_a_norm_stands_on_thin_evidence_that_agrees_with_itself(factory):
    """Six consistent weights are enough to call a 2 g steak a placeholder.

    This is the gap the whole failure went through: Flank Steak is stated six
    times corpus-wide, the old threshold wanted twenty, so the audit had no norm
    to judge the line against and treated 2 g as a fact.
    """
    with factory() as s:
        r = make_recipe(s, energy_kcal=523, protein_g=44, carbs_g=47, fat_g=19)
        r.ingredients = [make_ingredient("Flank Steak", 2, "grams", 2)]
        s.commit()
        seed_corpus_norm(s, "Flank Steak", 300, lines=6)

        blockers = audit.composition_blockers(r, audit.typical_weights(s, r))
        assert any("Flank Steak" in b and "typically 300" in b for b in blockers)


def test_a_placeholder_quantity_is_not_a_reason_to_rewrite_the_macros(factory):
    """The reported failure, end to end.

    Seared Steak with Crispy Potato Salad records its flank steak as 2 g. The
    audit summed the ingredients, got 140 kcal and 4.8 g of protein, and wrote
    all four source macros down to match — reporting a correction while the
    actual fault was a missing steak. The quantities have to be doubted first.
    """
    with factory() as s:
        r = make_recipe(s, energy_kcal=523, protein_g=44, carbs_g=47, fat_g=19)
        r.ingredients = [
            make_ingredient("Flank Steak", 2, "grams", 2),
            make_ingredient("Red Potato", 180, "grams", 180),
        ]
        s.commit()
        seed_corpus_norm(s, "Flank Steak", 300, lines=6)
        audit.flag_recipe(s, r.id)

        called = []

        def spy(system, user, schema):
            called.append(user)
            return {"ingredients": []}

        result = audit.audit_recipe(s, r.id, completer=spy)

        assert called == []  # not a question worth asking of a 2 g steak
        assert result.verdict == "inconclusive"
        assert result.findings == []
        assert any("Flank Steak" in g for g in result.ingredient_gaps)
        after = s.get(Recipe, r.id)
        assert (after.energy_kcal, after.protein_g) == (523, 44)  # untouched


def test_a_dish_lighter_than_its_plated_weight_cannot_price_its_macros(factory):
    """Every line plausible on its own, and the list still missing a third of the dish.

    The per-ingredient norms cannot see this — each weight is ordinary. Only the
    source's own serving weight says the list is incomplete.
    """
    with factory() as s:
        r = make_recipe(
            s, energy_kcal=800, protein_g=50, carbs_g=70, fat_g=30,
            serving_size_g=500,
            ingredients=[("Chicken", 300), ("Rice", 300)],
        )
        assert audit.check_mass_balance(r) is not None
        assert any("missing mass" in b for b in audit.composition_blockers(r, {}))


def test_a_dish_that_weighs_what_it_should_is_not_blocked(factory):
    with factory() as s:
        r = make_recipe(
            s, energy_kcal=800, protein_g=50, carbs_g=70, fat_g=30,
            serving_size_g=500,
            ingredients=[("Chicken", 500), ("Rice", 520)],
        )
        assert audit.check_mass_balance(r) is None


def test_no_stated_serving_weight_means_the_question_is_not_asked(factory):
    """Two thirds of the damage happened on recipes that state no plated weight.

    Absence of the check must read as "cannot say", never as "passed".
    """
    with factory() as s:
        r = make_recipe(
            s, energy_kcal=800, protein_g=50, carbs_g=70, fat_g=30,
            ingredients=[("Chicken", 2)],
        )
        assert audit.check_mass_balance(r) is None


def test_ingredients_that_do_not_add_up_to_a_meal_are_the_thing_thats_wrong(factory):
    """The last net, for when there is no evidence to check the quantities against.

    No corpus norm for the ingredient and no stated serving weight, so both
    earlier tests are silent. What remains is that 140 kcal is not a main course
    — and four macros agreeing with each other are better evidence than one
    ingredient list that does not.
    """
    with factory() as s:
        r = make_recipe(
            s, energy_kcal=523, protein_g=44, carbs_g=47, fat_g=19,
            ingredients=[("Obscure Cut", 2), ("Red Potato", 180)],
        )
        audit.flag_recipe(s, r.id)
        completer = constant_completer(
            kcal_per_100g=70, protein_per_100g=2, fat_per_100g=1, carbs_per_100g=14
        )
        result = audit.audit_recipe(s, r.id, completer=completer)

        assert result.verdict == "inconclusive"
        assert result.findings == []
        assert any("too little to be a meal" in g for g in result.ingredient_gaps)
        assert s.get(Recipe, r.id).protein_g == 44


def test_a_real_macro_error_is_still_corrected(factory):
    """The other case, which must keep working: quantities fine, macros wrong.

    Nothing above may turn the pass into one that never corrects anything —
    "inconclusive" is only the right answer when the ingredients cannot support
    the sum.
    """
    with factory() as s:
        r = make_recipe(
            s, energy_kcal=2100, protein_g=150, carbs_g=100, fat_g=120,
            serving_size_g=1000,
            ingredients=[("Chicken", 1000), ("Potatoes", 1000)],
        )
        completer = constant_completer(
            kcal_per_100g=100, protein_per_100g=10, fat_per_100g=5, carbs_per_100g=8
        )
        result = audit.audit_recipe(s, r.id, completer=completer, model="cheap-model")

        assert result.verdict == "corrected"
        assert s.get(Recipe, r.id).energy_kcal == pytest.approx(1000)


def test_equipment_is_not_a_missing_ingredient_weight(factory):
    """The source lists skewers among the ingredients; they have no weight to miss.

    Matched on word boundaries, or "cabbage" reads as containing "bag".
    """
    assert audit.is_non_food("Bamboo Skewers")
    assert audit.is_non_food("Wooden Skewers")
    assert not audit.is_non_food("Sweetheart Cabbage")
    assert not audit.is_non_food("Baby Leaves")

    with factory() as s:
        r = make_recipe(s, energy_kcal=540, protein_g=30, carbs_g=60, fat_g=20)
        r.ingredients = [
            make_ingredient("Bamboo Skewers", 1, "unit(s)", None),
            make_ingredient("Chicken", 500, "grams", 500),
        ]
        s.commit()
        assert audit.composition_blockers(r, audit.typical_weights(s, r)) == []


def test_will_not_compute_macros_from_a_partly_unweighed_recipe(factory):
    """Unweighed ingredients drag a computed total down, so refuse to use one.

    Without this the composition check would "correct" good macros to an
    underestimate built from whichever half of the recipe had weights.
    """
    with factory() as s:
        r = make_recipe(
            s,
            energy_kcal=2100, protein_g=150, carbs_g=100, fat_g=120,
            ingredients=[("Chicken", 1000), ("Boursin", None), ("New Potatoes", None)],
        )
        called = []

        def spy(system, user, schema):
            called.append(user)
            return {"ingredients": []}

        result = audit.audit_recipe(s, r.id, completer=spy)
        assert called == []  # the model is not asked an unanswerable question
        assert result.verdict == "inconclusive"
        assert result.findings == []
        assert s.get(Recipe, r.id).energy_kcal == 2100


def test_composition_needs_every_ingredient_priced(factory):
    """One unmatched ingredient invalidates the total it was left out of."""
    with factory() as s:
        r = make_recipe(
            s, energy_kcal=1000, protein_g=50, carbs_g=50, fat_g=50,
            ingredients=[("Chicken", 500), ("Rice", 500)],
        )
        partial = {"ingredients": [{
            "name": "Chicken", "kcal_per_100g": 100, "protein_per_100g": 10,
            "fat_per_100g": 5, "carbs_per_100g": 8,
        }]}
        assert audit.macros_from_composition(list(r.ingredients), partial, 2) is None


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@pytest.fixture
def client(factory, monkeypatch):
    from fastapi.testclient import TestClient

    import main
    from app.api.deps import get_session

    def override():
        with factory() as session:
            yield session

    # The background job builds its own session; point it at the test database.
    monkeypatch.setattr("app.api.deps._session_factory", lambda: factory)
    # ...and never let a test reach the network.
    monkeypatch.setattr(audit, "_default_completer", lambda: None)

    main.app.dependency_overrides[get_session] = override
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _await_job(client, job_id, tries=60):
    import time

    for _ in range(tries):
        job = client.get(f"/api/recipes/audit-jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("audit job did not finish")


def test_flag_endpoint_runs_the_audit_and_reports_corrections(client, factory):
    with factory() as s:
        r = make_recipe(s, energy_kcal=900, protein_g=30, carbs_g=60, fat_g=20)
        rid = r.id

    started = client.post(f"/api/recipes/{rid}/flag")
    assert started.status_code == 200
    job = _await_job(client, started.json()["job_id"])
    assert job["status"] == "done"
    assert job["result"]["verdict"] == "corrected"

    detail = client.get(f"/api/recipes/{rid}").json()
    assert detail["energy_kcal"] == pytest.approx(540)
    assert detail["flagged_suspicious"] is False  # the question has been answered
    assert detail["audited_at"] is not None
    assert [e["field"] for e in detail["edits"]] == ["energy_kcal"]


def test_revert_endpoint_restores_the_original(client, factory):
    with factory() as s:
        rid = make_recipe(s, energy_kcal=900, protein_g=30, carbs_g=60, fat_g=20).id
    _await_job(client, client.post(f"/api/recipes/{rid}/flag").json()["job_id"])

    reverted = client.post(f"/api/recipes/{rid}/revert")
    assert reverted.status_code == 200
    assert reverted.json()["energy_kcal"] == 900
    assert reverted.json()["edits"] == []


def test_flag_rejects_a_recipe_outside_the_curated_library(client, factory):
    with factory() as s:
        rid = make_recipe(s, curated=0, energy_kcal=900, protein_g=30, carbs_g=60, fat_g=20).id
    assert client.post(f"/api/recipes/{rid}/flag").status_code == 404
    assert client.post("/api/recipes/999999/flag").status_code == 404


def test_unknown_audit_job_is_a_404(client):
    assert client.get("/api/recipes/audit-jobs/nope").status_code == 404


def test_flagging_is_separate_from_the_computed_heuristic(factory):
    """The hand flag records that a person asked; macros_suspect stays computed."""
    with factory() as s:
        r = make_recipe(s, energy_kcal=540, protein_g=30, carbs_g=60, fat_g=20)
        audit.flag_recipe(s, r.id)
        assert s.get(Recipe, r.id).flagged_suspicious == 1

        audit.audit_recipe(s, r.id, completer=None)
        after = s.get(Recipe, r.id)
        # The question has been answered, so the flag clears; the audit stamp stays.
        assert after.flagged_suspicious == 0
        assert after.audited_at is not None


# --------------------------------------------------------------------------
# Answering a human's report
# --------------------------------------------------------------------------

def _risotto(session):
    """Mushroom and Pancetta Risotto (16571): 79 g of protein, ~16 g of ingredients.

    The shape that defeated every heuristic — the macros agree with each other to
    3%, the dish is not vegetarian, and 10.8 g of protein per 100 kcal sits under
    the "more than food allows" line.
    """
    return make_recipe(
        session,
        energy_kcal=729, protein_g=79, carbs_g=87, fat_g=10,
        base_yield=4,
        ingredients=[
            ("Leek", 150), ("Vine Tomatoes", 180), ("Chestnut Mushrooms", 150),
            ("Risotto Rice", 350), ("British Smoked Bacon Lardons", 180),
            ("Mangetout", 80),
        ],
    )


def test_a_hand_flagged_recipe_is_checked_even_when_nothing_looks_wrong(factory):
    """The heuristics decide whether to spend a model call unprompted.

    They are not a second opinion on a person's report. Someone who flags a recipe
    has already supplied the reason to look.
    """
    with factory() as s:
        r = _risotto(s)
        assert audit.check_macro_arithmetic(r) == []
        assert audit.check_plausibility(r) == []  # nothing a heuristic can see

        audit.flag_recipe(s, r.id)
        # ~1.5 g protein per 100 g across the board: nowhere near 79 g a serving.
        completer = constant_completer(
            kcal_per_100g=120, protein_per_100g=1.5, fat_per_100g=1, carbs_per_100g=20
        )
        result = audit.audit_recipe(s, r.id, completer=completer)
        assert result.used_llm is True
        assert result.verdict == "corrected"
        assert s.get(Recipe, r.id).protein_g < 20


def test_an_unflagged_recipe_with_nothing_wrong_costs_no_model_call(factory):
    """The cost gate still holds for anything nobody has complained about."""
    with factory() as s:
        r = _risotto(s)

        def explode(system, user, schema):  # pragma: no cover - must not be called
            raise AssertionError("no model call should be made")

        result = audit.audit_recipe(s, r.id, completer=explode)
        assert result.used_llm is False
        assert result.verdict == "ok"


def test_a_name_echoed_with_its_weight_still_matches(factory):
    """The prompt shows "- Leek (150 g)", so the echo sometimes carries the weight.

    Matching on the exact string meant a whole answer went unmatched, which the
    audit then reported as "looks correct".
    """
    with factory() as s:
        r = _risotto(s)
        audit.flag_recipe(s, r.id)

        def echoes_the_weight(system, user, schema):
            return {"ingredients": [
                {
                    "name": f"{i.name} ({i.amount_g:.0f} g)",
                    "kcal_per_100g": 120, "protein_per_100g": 1.5,
                    "fat_per_100g": 1, "carbs_per_100g": 20,
                }
                for i in r.ingredients
            ]}

        result = audit.audit_recipe(s, r.id, completer=echoes_the_weight)
        assert result.verdict == "corrected"
        assert s.get(Recipe, r.id).protein_g < 20


def test_an_answer_it_could_not_use_is_inconclusive_not_ok(factory):
    """Failing to check must never read the same as having checked and found nothing."""
    with factory() as s:
        r = _risotto(s)
        audit.flag_recipe(s, r.id)

        def unusable(system, user, schema):
            return {"ingredients": [{
                "name": "Something Else Entirely", "kcal_per_100g": 100,
                "protein_per_100g": 10, "fat_per_100g": 5, "carbs_per_100g": 8,
            }]}

        result = audit.audit_recipe(s, r.id, completer=unusable)
        assert result.verdict == "inconclusive"
        assert result.findings == []
        assert s.get(Recipe, r.id).protein_g == 79  # nothing guessed at
