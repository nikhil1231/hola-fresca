"""Basket + coverage behaviour for aliases and pantry staples.

The point of aliasing: two recipe ingredients that are really the same thing must
sum into one pack, not be bought twice.
"""
from __future__ import annotations

import csv

from app.db.models import Recipe, RecipeIngredient
from app.mapping import coverage, service
from app.mapping.candidates import gather_candidates

from tests.conftest import seed_candidates

PESTO = [
    {"sku": "pesto1", "name": "Sacla Basil Pesto 190g", "price": 2.5, "pack_value": 190, "pack_unit": "g"}
]
CANON, ALIAS = "name:basil pesto", "name:fresh pesto"
SID_CANON, SID_ALIAS = "sid-canon", "sid-alias"


def _csv(tmp_path):
    """A minimal frequency CSV mapping the two source ids to their keys."""
    path = tmp_path / "freq.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "rank", "ingredient_key", "source_ingredient_ids", "name", "recipe_count",
                "recipe_pct", "line_count", "metric_unit", "metric_known_pct",
                "median_metric_amount", "mean_metric_amount", "p25_metric_amount",
                "p75_metric_amount", "common_native_amounts", "name_variants",
            ],
        )
        w.writeheader()
        for i, (key, sid, name) in enumerate(
            [(CANON, SID_CANON, "Basil Pesto"), (ALIAS, SID_ALIAS, "Fresh Pesto")], start=1
        ):
            w.writerow({
                "rank": i, "ingredient_key": key, "source_ingredient_ids": sid, "name": name,
                "recipe_count": 1, "recipe_pct": 0, "line_count": 100, "metric_unit": "g",
                "metric_known_pct": 100, "median_metric_amount": 100, "mean_metric_amount": 100,
                "p25_metric_amount": 100, "p75_metric_amount": 100,
                "common_native_amounts": "", "name_variants": "",
            })
    return path


def _seed(factory):
    with factory() as s:
        seed_candidates(s, CANON, "Basil Pesto", PESTO, line_count=200)
        seed_candidates(s, ALIAS, "Fresh Pesto", PESTO, line_count=90)
        for key in (CANON, ALIAS):
            service.save_decision(
                s,
                gather_candidates(s, key),
                service.DecisionInput(
                    status="approved", accepted=[service.AcceptedInput(sku="pesto1", rank=1)]
                ),
            )
        # One recipe using both names, 100 g each.
        recipe = Recipe(
            source="hellofresh", source_id="r1", url="", name="Pesto Pasta", curated=1,
            ingredients=[
                RecipeIngredient(name="Basil Pesto", source_ingredient_id=SID_CANON,
                                 amount=100, unit="grams", amount_g=100),
                RecipeIngredient(name="Fresh Pesto", source_ingredient_id=SID_ALIAS,
                                 amount=100, unit="grams", amount_g=100),
            ],
        )
        s.add(recipe)
        s.commit()
        return recipe.id


def test_unaliased_duplicates_are_bought_twice(factory, tmp_path):
    rid = _seed(factory)
    basket = coverage.build_basket(factory, [rid], csv_path=_csv(tmp_path))
    # Two separate lines, one pack each = two jars.
    assert len(basket.lines) == 2
    assert sum(line.packs for line in basket.lines) == 2
    assert basket.total == 5.0


def test_aliased_duplicates_sum_into_one_pack(factory, tmp_path):
    rid = _seed(factory)
    with factory() as s:
        service.set_alias(s, ALIAS, CANON)

    basket = coverage.build_basket(factory, [rid], csv_path=_csv(tmp_path))
    # One line for 200 g, still one 190 g jar short -> 2 packs, but a single line
    # and the demand is pooled rather than double-counted as two separate jars.
    assert len(basket.lines) == 1
    line = basket.lines[0]
    assert line.ingredient_key == CANON
    assert line.need_g == 200


def test_pantry_staples_count_as_covered(factory, tmp_path):
    rid = _seed(factory)
    with factory() as s:
        # A staple with no accepted products at all still counts as handled.
        service.save_decision(
            s,
            gather_candidates(s, ALIAS),
            service.DecisionInput(status="approved", accepted=[], pantry_staple=True),
        )
    rep = coverage.coverage_report(factory, csv_path=_csv(tmp_path))
    assert rep.lines_resolved == rep.lines_total == 2
