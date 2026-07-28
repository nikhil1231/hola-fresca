"""Tests for unit backfill and gram conversion."""
from __future__ import annotations

import pytest

from app import config as app_config
from app.canonicalize import (
    backfill_units,
    repair_contradicted_units,
    repair_trace_amounts,
    to_grams,
)
from app.db.models import Recipe, RecipeIngredient
from app.db.session import init_db, make_engine, make_session_factory


@pytest.mark.parametrize(
    "name,amount,unit,expected_g,expected_unit",
    [
        ("Baby Spinach", 100, "grams", 100, "g"),
        ("Water", 50, "milliliter(s)", 50, "ml"),
        ("Garlic Clove", 1, "unit(s)", 5.0, "g"),
        ("Lentils", 1, "carton(s)", 250.0, "g"),          # not "1 carton"
        # Stock is a small dose of concentrate. It used to weigh 160 g here,
        # because the keyword table matched "chicken" for the poultry variant and
        # priced a sachet as a chicken breast; the sibling entries came along for
        # the ride at the old flat 8 g default.
        ("Vegetable Stock Powder", 1, "sachet(s)", 11.0, "g"),
        ("Chicken Stock Powder", 1, "sachet(s)", 11.0, "g"),
        ("British Chicken Breasts", 2, "unit(s)", 320.0, "g"),
        ("Ground Cumin", 1, "tsp", 5.0, "g"),
        ("Olive Oil", 2, "tbsp", 30.0, "g"),
        # Herb bunches are calibrated per name: the same herbs appear as plain
        # gram lines elsewhere in the corpus and cluster at 8-10 g, well under
        # the 25 g generic bunch default they used to fall back to.
        ("Coriander", 1, "bunch(es)", 10.0, "g"),
        ("Chives", 1, "bunch(es)", 8.0, "g"),
        ("Lasagne Sheets", 1, "pack(s)", 250.0, "g"),      # generic by_unit fallback
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
        assert updated == {"by_id": 0, "by_name": 1, "by_magnitude": 1, "count_veto": 0}

        rows = {(i.name, i.unit) for i in s.query(RecipeIngredient).all()}
        # Adopted from the sibling id carrying the same name.
        assert ("Smoked Paprika", "sachet(s)") in rows
        # No unit anywhere and a small amount, so it reads as a count.
        assert ("Red Pepper", "unit(s)") in rows


def _repair_fixture(tmp_path, monkeypatch, lines):
    monkeypatch.setattr(app_config, "DB_PATH", tmp_path / "r.db")
    engine = make_engine(tmp_path / "r.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        r = Recipe(source="hellofresh", source_id="x", url="u", name="R")
        r.ingredients = [
            RecipeIngredient(source_ingredient_id=f"i{n}", name=name, amount=amount, unit=unit)
            for n, (name, amount, unit) in enumerate(lines)
        ]
        s.add(r)
        s.commit()
        stats = repair_contradicted_units(s)
        s.commit()
        return s, stats


def test_repair_rereads_a_count_the_source_labelled_as_grams(tmp_path, monkeypatch):
    """"2 grams" of a thing sold in nests means two nests.

    The corpus states 125 g elsewhere for the same name, so 2 nests (120 g) is
    the reading that agrees with it.
    """
    s, stats = _repair_fixture(tmp_path, monkeypatch, [
        ("Egg Noodle Nest", 125, "grams"),    # a line the source labelled properly
        ("Egg Noodle Nest", 3, "nest(s)"),    # ...and one that names the count unit
        ("Egg Noodle Nest", 2, "grams"),      # the unlabelled one, mis-backfilled
    ])
    assert stats["repaired"] == 1
    units = {(i.amount, i.unit) for i in s.query(RecipeIngredient).all()}
    assert (2, "nest(s)") in units
    assert (125, "grams") in units  # the real weight is untouched


def test_repair_leaves_genuinely_small_weights_alone(tmp_path, monkeypatch):
    """5 g of sesame seeds is real: nothing in the corpus ever counts them."""
    s, stats = _repair_fixture(tmp_path, monkeypatch, [
        ("Roasted White Sesame Seeds", 20, "grams"),
        ("Roasted White Sesame Seeds", 5, "grams"),
    ])
    assert stats["repaired"] == 0
    assert all(i.unit == "grams" for i in s.query(RecipeIngredient).all())


def test_repair_rejects_a_count_unit_that_means_a_whole_bottle(tmp_path, monkeypatch):
    """"1 Balsamic Vinegar" is not a 250 ml bottle, so leave the line as it is.

    Its only countable unit is ``pack(s)``, which converts to far more than the
    corpus ever uses in one recipe — the guard that stops a plausible-looking
    re-read from inventing a huge quantity.
    """
    s, stats = _repair_fixture(tmp_path, monkeypatch, [
        ("Balsamic Vinegar", 15, "milliliter(s)"),
        ("Balsamic Vinegar", 1, "pack(s)"),
        ("Balsamic Vinegar", 1, "milliliter(s)"),
    ])
    assert stats["repaired"] == 0


def test_repair_needs_a_trustworthy_reference_weight(tmp_path, monkeypatch):
    """With no stated weight for the name, there is nothing to check against."""
    s, stats = _repair_fixture(tmp_path, monkeypatch, [
        ("Mystery Item", 1, "sachet(s)"),
        ("Mystery Item", 2, "grams"),
    ])
    assert stats["repaired"] == 0


def _trace_fixture(tmp_path, monkeypatch, lines):
    monkeypatch.setattr(app_config, "DB_PATH", tmp_path / "t.db")
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        r = Recipe(source="hellofresh", source_id="x", url="u", name="R")
        r.ingredients = [
            RecipeIngredient(source_ingredient_id=f"i{n}", name=name, amount=amount, unit=unit)
            for n, (name, amount, unit) in enumerate(lines)
        ]
        s.add(r)
        s.commit()
        stats = repair_trace_amounts(s)
        s.commit()
        return s, stats


def test_trace_amount_reads_as_one_typical_portion(tmp_path, monkeypatch):
    """"Green Beans, 1 g" means one portion, and the corpus says that is 150 g.

    No countable unit exists for green beans anywhere, so the unit repair cannot
    reach this line; its own median weight is the only evidence available.
    """
    lines = [("Green Beans", 150, "grams")] * 20 + [("Green Beans", 1, "grams")]
    s, stats = _trace_fixture(tmp_path, monkeypatch, lines)
    assert stats["repaired"] == 1
    amounts = sorted(i.amount for i in s.query(RecipeIngredient).all())
    assert amounts[0] == 150  # the 1 g line became a full portion
    assert all(a == 150 for a in amounts)


def test_trace_amount_scales_a_fractional_portion(tmp_path, monkeypatch):
    lines = [("Green Beans", 150, "grams")] * 20 + [("Green Beans", 0.5, "grams")]
    s, _ = _trace_fixture(tmp_path, monkeypatch, lines)
    assert sorted(i.amount for i in s.query(RecipeIngredient).all())[0] == 75


def test_trace_repair_needs_real_evidence_for_the_portion(tmp_path, monkeypatch):
    """Two supporting lines is not a norm, so the placeholder is left as it is."""
    lines = [("Green Beans", 150, "grams")] * 2 + [("Green Beans", 1, "grams")]
    s, stats = _trace_fixture(tmp_path, monkeypatch, lines)
    assert stats["repaired"] == 0


def test_trace_repair_refuses_an_absurd_multiplier(tmp_path, monkeypatch):
    """A stray 4 must not become four portions of anything."""
    lines = [("Green Beans", 150, "grams")] * 20 + [("Green Beans", 4, "grams")]
    s, stats = _trace_fixture(tmp_path, monkeypatch, lines)
    assert stats["repaired"] == 0


def test_trace_repair_is_idempotent(tmp_path, monkeypatch):
    lines = [("Green Beans", 150, "grams")] * 20 + [("Green Beans", 1, "grams")]
    s, _ = _trace_fixture(tmp_path, monkeypatch, lines)
    again = repair_trace_amounts(s)
    assert again["repaired"] == 0


def test_to_grams_rejects_implausible_counts():
    """A count unit on a large amount is a mislabelled gram weight."""
    # 670 shanks would be 100kg of lamb; the source means 670g.
    assert to_grams("Lamb Shank", 670, "unit(s)") == (670, "g")
    assert to_grams("Butternut Squash", 1000, "unit(s)") == (1000, "g")
    # Plausible counts still use the gram reference.
    assert to_grams("Butternut Squash", 1, "unit(s)") == (550.0, "g")
