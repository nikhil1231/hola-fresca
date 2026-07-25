"""Tests for unit backfill and gram conversion."""
from __future__ import annotations

import pytest

from app import config as app_config
from app.canonicalize import backfill_units, to_grams
from app.db.models import Recipe, RecipeIngredient
from app.db.session import init_db, make_engine, make_session_factory


@pytest.mark.parametrize(
    "name,amount,unit,expected_g,expected_unit",
    [
        ("Baby Spinach", 100, "grams", 100, "g"),
        ("Water", 50, "milliliter(s)", 50, "ml"),
        ("Garlic Clove", 1, "unit(s)", 5.0, "g"),
        ("Lentils", 1, "carton(s)", 250.0, "g"),          # not "1 carton"
        ("Vegetable Stock Powder", 1, "sachet(s)", 8.0, "g"),
        ("British Chicken Breasts", 2, "unit(s)", 320.0, "g"),
        ("Ground Cumin", 1, "tsp", 5.0, "g"),
        ("Olive Oil", 2, "tbsp", 30.0, "g"),
        ("Coriander", 1, "bunch(es)", 25.0, "g"),          # generic by_unit fallback
    ],
)
def test_to_grams(name, amount, unit, expected_g, expected_unit):
    grams, cunit = to_grams(name, amount, unit)
    assert grams == expected_g
    assert cunit == expected_unit


def test_to_grams_unresolvable():
    # Non-food / unknown ingredient with a count unit and no reference entry.
    assert to_grams("Bamboo Skewers", 6, "unit(s)") == (None, None)
    # Missing amount or unit.
    assert to_grams("Lentils", None, "carton(s)") == (None, None)
    assert to_grams("Lentils", 1, None) == (None, None)


def test_backfill_units_uses_modal(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "DB_PATH", tmp_path / "c.db")
    engine = make_engine(tmp_path / "c.db")
    init_db(engine)
    factory = make_session_factory(engine)

    with factory() as s:
        r = Recipe(source="hellofresh", source_id="x", url="u", name="R")
        # Same ingredient id "lent": grams in two recipes... here modal is carton.
        r.ingredients = [
            RecipeIngredient(source_ingredient_id="lent", name="Lentils", amount=1, unit="carton(s)"),
            RecipeIngredient(source_ingredient_id="lent", name="Lentils", amount=1, unit="carton(s)"),
            RecipeIngredient(source_ingredient_id="lent", name="Lentils", amount=1, unit=""),  # empty
            RecipeIngredient(source_ingredient_id="spin", name="Spinach", amount=80, unit=None),  # no corpus unit
        ]
        s.add(r)
        s.commit()

        updated = backfill_units(s)
        s.commit()
        assert updated["by_id"] == 1  # only the empty lentils row matches on id

        rows = {(i.name, i.unit) for i in s.query(RecipeIngredient).all()}
        assert ("Lentils", "carton(s)") in rows
        # Spinach has no unit anywhere in the corpus, so its amount decides:
        # 80 is far too large to be a count, and reads as grams.
        assert updated["by_magnitude"] == 1
        assert ("Spinach", "grams") in rows


def test_backfill_units_falls_back_to_name(tmp_path, monkeypatch):
    """Ingredient ids are versioned, so the unit often sits under a sibling id."""
    monkeypatch.setattr(app_config, "DB_PATH", tmp_path / "c.db")
    engine = make_engine(tmp_path / "c.db")
    init_db(engine)
    factory = make_session_factory(engine)

    with factory() as s:
        r = Recipe(source="hellofresh", source_id="x", url="u", name="R")
        r.ingredients = [
            RecipeIngredient(source_ingredient_id="pap-1", name="Smoked Paprika", amount=1, unit="sachet(s)"),
            RecipeIngredient(source_ingredient_id="pap-2", name="Smoked Paprika", amount=1, unit=None),
            RecipeIngredient(source_ingredient_id="pep-1", name="Red Pepper", amount=2, unit=None),
        ]
        s.add(r)
        s.commit()

        updated = backfill_units(s)
        s.commit()
        assert updated == {"by_id": 0, "by_name": 1, "by_magnitude": 1}

        rows = {(i.name, i.unit) for i in s.query(RecipeIngredient).all()}
        # Adopted from the sibling id carrying the same name.
        assert ("Smoked Paprika", "sachet(s)") in rows
        # No unit anywhere and a small amount, so it reads as a count.
        assert ("Red Pepper", "unit(s)") in rows


def test_to_grams_rejects_implausible_counts():
    """A count unit on a large amount is a mislabelled gram weight."""
    # 670 shanks would be 100kg of lamb; the source means 670g.
    assert to_grams("Lamb Shank", 670, "unit(s)") == (670, "g")
    assert to_grams("Butternut Squash", 1000, "unit(s)") == (1000, "g")
    # Plausible counts still use the gram reference.
    assert to_grams("Butternut Squash", 1, "unit(s)") == (550.0, "g")
