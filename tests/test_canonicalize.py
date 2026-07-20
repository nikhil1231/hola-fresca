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
        assert updated == 1  # only the empty lentils row gets a unit

        rows = {(i.name, i.unit) for i in s.query(RecipeIngredient).all()}
        assert ("Lentils", "carton(s)") in rows
        # Spinach had no other occurrence with a unit, so it stays empty.
        assert ("Spinach", None) in rows
