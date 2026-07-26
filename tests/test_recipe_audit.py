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
