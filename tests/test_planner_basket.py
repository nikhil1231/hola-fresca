"""Planner basket engine: pack covering, £-valued waste, and form preference.

The covering tests work on synthetic packs because the behaviour worth pinning
down is arithmetic, not plumbing; the tail of the file exercises the real
DB-backed index so the two cannot drift apart.
"""
from __future__ import annotations

import csv

import pytest

from sqlalchemy import select

from app.db.models import IngredientMapping, Recipe, RecipeIngredient
from app.mapping import service
from app.mapping.candidates import gather_candidates
from app.planner import basket as B
from app.planner import waste as W
from app.planner.index import (
    Ingredient,
    Pack,
    PlanIndex,
    derive_count_metadata,
    load_index,
)

from tests.conftest import seed_candidates

CSV_FIELDS = [
    "rank", "ingredient_key", "source_ingredient_ids", "name", "recipe_count", "recipe_pct",
    "line_count", "metric_unit", "metric_known_pct", "median_metric_amount",
    "mean_metric_amount", "p25_metric_amount", "p75_metric_amount", "common_native_amounts",
    "name_variants",
]


def write_freq_csv(path, rows):
    """Minimal ingredient_frequency.csv: the source id -> ingredient key index."""
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for i, (key, sid, name) in enumerate(rows, start=1):
            writer.writerow({
                "rank": i, "ingredient_key": key, "source_ingredient_ids": sid, "name": name,
                "recipe_count": 1, "recipe_pct": 0, "line_count": 1, "metric_unit": "g",
                "metric_known_pct": 100, "median_metric_amount": 100, "mean_metric_amount": 100,
                "p25_metric_amount": 100, "p75_metric_amount": 100,
                "common_native_amounts": "", "name_variants": "",
            })
    return path


def pack(capacity_g, price, *, salvage=0.85, match_type="exact", sku=None):
    label = sku or f"{capacity_g:g}g"
    return Pack(
        sku=label, product_name=f"pack {label}", capacity_g=capacity_g, price=price,
        salvage=salvage, rank=1, match_type=match_type, pack_size_raw=label,
    )


def ingredient(*packs, key="name:test", staple=False):
    return Ingredient(key=key, name="Test", pantry_staple=staple, packs=tuple(packs))


# --------------------------------------------------------------------------
# Salvage model
# --------------------------------------------------------------------------

def test_ambient_goods_salvage_more_than_fresh():
    """The same leftover is a different loss depending on whether it keeps."""
    herbs = W.salvage_fraction(shelf_life_days=2, category="Fresh & Chilled Food > Herbs")
    tins = W.salvage_fraction(shelf_life_days=None, category="Food Cupboard > Tinned")
    assert herbs == 0.0
    assert tins > 0.8


def test_missing_shelf_life_is_not_treated_as_short():
    """Ocado states no life for ambient stock; that must not read as perishable."""
    assert W.salvage_fraction(None, "Food Cupboard > Cooking Ingredients") > 0.5


def test_frozen_overrides_a_short_stated_life():
    assert W.salvage_fraction(2, "Frozen Food > Vegetables") == pytest.approx(0.90)


def test_waste_value_prices_the_remainder_at_what_it_cost():
    # Half of a £4 pack left, none of it salvageable -> £2 binned.
    assert W.waste_value(leftover_g=250, cost=4.0, capacity_g=500, salvage=0.0) == pytest.approx(2.0)
    # No leftover, no waste, whatever the ingredient is.
    assert W.waste_value(0, 4.0, 500, 0.0) == pytest.approx(0.0)


def test_even_a_keeps_forever_leftover_carries_some_waste():
    """The salvage ceiling: a half-used jar you might never finish is not free."""
    assert W.waste_value(250, 4.0, 500, salvage=1.0) == pytest.approx(
        2.0 * (1 - W.SALVAGE_CEILING)
    )


# --------------------------------------------------------------------------
# Covering
# --------------------------------------------------------------------------

def test_cover_prefers_one_big_pack_over_several_small():
    cover = B._cover_with_packs((pack(500, 1.00), pack(2000, 2.50)), need_g=1500)
    assert cover.packs == 1
    assert cover.choices[0].pack.capacity_g == 2000
    assert cover.cost == pytest.approx(2.50)


def test_cover_mixes_pack_sizes_when_that_is_cheaper():
    """2.1 kg is best served by a big bag plus a small one, not two big ones."""
    cover = B._cover_with_packs((pack(500, 1.00), pack(2000, 2.50)), need_g=2100)
    assert sorted(c.pack.capacity_g for c in cover.choices) == [500, 2000]
    assert cover.cost == pytest.approx(3.50)


def test_cover_pays_more_cash_to_avoid_wasting_something_perishable():
    """With nothing salvageable, a tight cover beats a cheaper oversized one."""
    small, big = pack(30, 1.00, salvage=0.0), pack(100, 1.60, salvage=0.0)
    cover = B._cover_with_packs((small, big), need_g=50)
    assert cover.packs == 2 and cover.choices[0].pack.capacity_g == 30
    assert cover.cost > big.price  # more money out of pocket...
    assert cover.score < B._score_multiset([B.PackChoice(big, 1)], 50).score  # ...less thrown away


def test_cover_uses_the_cheap_big_pack_when_leftovers_keep():
    """Same shapes as above, but ambient: now the oversized pack is correct."""
    small, big = pack(30, 1.00, salvage=0.85), pack(100, 1.60, salvage=0.85)
    cover = B._cover_with_packs((small, big), need_g=50)
    assert cover.packs == 1 and cover.choices[0].pack.capacity_g == 100


def test_exact_form_wins_even_when_a_substitute_is_far_cheaper():
    """Lime juice is pennies per gram next to real limes; it is still a fallback.

    Without this, a pure cost objective quietly rewrites every recipe to its
    cheapest degraded form.
    """
    limes = pack(130, 0.75, match_type="exact", sku="limes")
    juice = pack(250, 0.80, match_type="form_differs", sku="juice")
    cover = B.cover_need(PlanIndex(), ingredient(limes, juice), need_g=65)
    assert cover.choices[0].pack.sku == "limes"


def test_substitute_is_used_when_no_exact_form_is_buyable():
    juice = pack(250, 0.80, match_type="form_differs", sku="juice")
    cover = B.cover_need(PlanIndex(), ingredient(juice), need_g=65)
    assert cover.choices[0].pack.sku == "juice"


def test_no_cover_without_packs_or_demand():
    assert B.cover_need(PlanIndex(), ingredient(), need_g=100) is None
    assert B.cover_need(PlanIndex(), ingredient(pack(500, 1.0)), need_g=0) is None


# --------------------------------------------------------------------------
# Basket assembly, against the real index
# --------------------------------------------------------------------------

RICE = [{"sku": "rice1", "name": "Basmati 500g", "price": 1.05, "pack_value": 500, "pack_unit": "g"}]
SALT = [{"sku": "salt1", "name": "Table Salt 750g", "price": 0.65, "pack_value": 750, "pack_unit": "g"}]

KEY_RICE, KEY_PILAU, KEY_SALT = "name:basmati rice", "name:pilau rice", "name:salt"
SID_RICE, SID_PILAU, SID_SALT = "sid-rice", "sid-pilau", "sid-salt"


@pytest.fixture
def seeded(factory, tmp_path):
    """Two names for rice plus a staple, used by one 2-serving recipe."""
    csv_path = write_freq_csv(tmp_path / "freq.csv", [
        (KEY_RICE, SID_RICE, "Basmati Rice"),
        (KEY_PILAU, SID_PILAU, "Pilau Rice"),
        (KEY_SALT, SID_SALT, "Salt"),
    ])
    with factory() as s:
        seed_candidates(s, KEY_RICE, "Basmati Rice", RICE)
        seed_candidates(s, KEY_PILAU, "Pilau Rice", RICE)
        seed_candidates(s, KEY_SALT, "Salt", SALT)
        for key in (KEY_RICE, KEY_PILAU):
            service.save_decision(
                s, gather_candidates(s, key),
                service.DecisionInput(
                    status="approved", accepted=[service.AcceptedInput(sku="rice1", rank=1)]
                ),
            )
        service.save_decision(
            s, gather_candidates(s, KEY_SALT),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="salt1", rank=1)],
                pantry_staple=True,
            ),
        )
        recipe = Recipe(
            source="hellofresh", source_id="r1", url="", name="Rice Bowl", curated=1, base_yield=2,
            ingredients=[
                RecipeIngredient(name="Basmati Rice", source_ingredient_id=SID_RICE,
                                 amount=150, unit="grams", amount_g=150),
                RecipeIngredient(name="Pilau Rice", source_ingredient_id=SID_PILAU,
                                 amount=150, unit="grams", amount_g=150),
                RecipeIngredient(name="Salt", source_ingredient_id=SID_SALT,
                                 amount=2, unit="grams", amount_g=2),
            ],
        )
        s.add(recipe)
        s.commit()
        rid = recipe.id
    return factory, csv_path, rid


def test_pantry_staples_are_assumed_owned(seeded):
    factory, csv_path, rid = seeded
    index = load_index(factory, csv_path=csv_path)
    result = B.build_basket(index, [B.Selection(rid)])
    assert "Salt" in result.staples
    assert all(line.name != "Salt" for line in result.lines)


def test_aliased_names_pool_into_one_pack(seeded):
    """300 g of "rice" under two names is two packs; unaliased it would be four."""
    factory, csv_path, rid = seeded
    with factory() as s:
        service.set_alias(s, KEY_PILAU, KEY_RICE)

    index = load_index(factory, csv_path=csv_path)
    result = B.build_basket(index, [B.Selection(rid)])
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.need_g == pytest.approx(300)
    assert line.cover.packs == 1  # one 500 g bag covers the pooled demand


def test_servings_scale_demand_off_the_base_yield(seeded):
    factory, csv_path, rid = seeded
    with factory() as s:
        service.set_alias(s, KEY_PILAU, KEY_RICE)
    index = load_index(factory, csv_path=csv_path)

    base = B.build_basket(index, [B.Selection(rid)])
    doubled = B.build_basket(index, [B.Selection(rid, servings=4)])
    assert doubled.lines[0].need_g == pytest.approx(base.lines[0].need_g * 2)
    assert doubled.cost > base.cost
    assert doubled.lines[0].contributions[0].grams == pytest.approx(
        base.lines[0].contributions[0].grams * 2
    )


def test_repeated_recipes_sum_into_shared_packs(seeded):
    """Cooking the same thing twice must not buy two half-used bags."""
    factory, csv_path, rid = seeded
    with factory() as s:
        service.set_alias(s, KEY_PILAU, KEY_RICE)
    index = load_index(factory, csv_path=csv_path)

    once = B.build_basket(index, [B.Selection(rid)])
    twice = B.build_basket(index, [B.Selection(rid), B.Selection(rid)])
    assert twice.lines[0].need_g == pytest.approx(600)
    assert twice.lines[0].contributions[0].grams == pytest.approx(600)
    # 600 g needs two bags, not the four a per-recipe basket would have bought.
    assert twice.lines[0].cover.packs == 2


def test_trace_demands_are_reported_separately(seeded):
    """A pack bought for 2 g of something is a data artefact, not a shop."""
    factory, csv_path, rid = seeded
    with factory() as s:
        # Un-staple the salt so it reaches the basket as a real line.
        service.save_decision(
            s, gather_candidates(s, KEY_SALT),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="salt1", rank=1)],
                pantry_staple=False,
            ),
        )
    index = load_index(factory, csv_path=csv_path)
    result = B.build_basket(index, [B.Selection(rid)])
    assert [line.name for line in result.trace_lines] == ["Salt"]


def test_unmapped_ingredients_are_listed_not_silently_dropped(factory, tmp_path):
    csv_path = write_freq_csv(tmp_path / "freq.csv", [(KEY_RICE, SID_RICE, "Basmati Rice")])
    with factory() as s:
        recipe = Recipe(
            source="hellofresh", source_id="r2", url="", name="Plain Rice", curated=1, base_yield=2,
            ingredients=[
                RecipeIngredient(name="Basmati Rice", source_ingredient_id=SID_RICE,
                                 amount=150, unit="grams", amount_g=150),
            ],
        )
        s.add(recipe)
        s.commit()
        rid = recipe.id

    index = load_index(factory, csv_path=csv_path)
    result = B.build_basket(index, [B.Selection(rid)])
    assert result.unmapped == ["Basmati Rice"]
    assert result.cost == 0.0


def test_zero_amount_recipe_lines_do_not_become_planner_gaps(factory, tmp_path):
    csv_path = write_freq_csv(
        tmp_path / "freq.csv",
        [
            (KEY_RICE, SID_RICE, "Basmati Rice"),
            ("name:ghost spice", "sid-ghost", "Ghost Spice"),
        ],
    )
    with factory() as s:
        seed_candidates(s, KEY_RICE, "Basmati Rice", RICE)
        service.save_decision(
            s,
            gather_candidates(s, KEY_RICE),
            service.DecisionInput(
                status="approved", accepted=[service.AcceptedInput(sku="rice1", rank=1)]
            ),
        )
        recipe = Recipe(
            source="hellofresh", source_id="r-zero", url="", name="Rice With Ghost", curated=1,
            base_yield=2,
            ingredients=[
                RecipeIngredient(name="Basmati Rice", source_ingredient_id=SID_RICE,
                                 amount=150, unit="grams", amount_g=150),
                RecipeIngredient(name="Ghost Spice", source_ingredient_id="sid-ghost",
                                 amount=0, unit="sachet(s)", amount_g=0),
            ],
        )
        s.add(recipe)
        s.commit()
        rid = recipe.id

    index = load_index(factory, csv_path=csv_path)
    plan_recipe = index.recipes[rid]
    assert [need.display_name for need in plan_recipe.needs] == ["Basmati Rice"]
    assert plan_recipe.untracked_lines == 0

    result = B.build_basket(index, [B.Selection(rid)])
    assert result.unmapped == []
    assert result.untracked_lines == 0
    assert B.basket_gap_count(result) == 0


# --------------------------------------------------------------------------
# Unit-space covering
# --------------------------------------------------------------------------

def test_count_ingredient_covers_in_units_not_grams(factory, tmp_path):
    key, sid = "name:basa fillets", "sid-basa"
    csv_path = write_freq_csv(tmp_path / "freq.csv", [(key, sid, "Basa Fillets")])
    with factory() as s:
        seed_candidates(
            s,
            key,
            "Basa Fillets",
            [{
                "sku": "basa-250", "name": "Ocado 2 Basa Fillets 250g", "price": 3.0,
                "pack_raw": "250g", "pack_value": 250, "pack_unit": "g",
            }],
        )
        service.save_decision(
            s,
            gather_candidates(s, key),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="basa-250", rank=1)],
            ),
        )
        recipe = Recipe(
            source="hellofresh", source_id="basa", url="", name="Basa Tacos", curated=1,
            base_yield=2,
            ingredients=[
                RecipeIngredient(
                    name="Basa Fillets", source_ingredient_id=sid, amount=4,
                    unit="unit(s)", amount_g=520,
                ),
            ],
        )
        s.add(recipe)
        # Siblings so the ingredient earns its count classification: one line is no
        # longer enough to make something countable, or five stray "unit(s)" lines
        # would go on turning rosemary into sprigs.
        for i in range(2):
            s.add(Recipe(
                source="hellofresh", source_id=f"basa-sib{i}", url="", name=f"Basa {i}",
                curated=1, base_yield=2,
                ingredients=[RecipeIngredient(
                    name="Basa Fillets", source_ingredient_id=sid, amount=2,
                    unit="unit(s)", amount_g=260,
                )],
            ))
        s.commit()
        rid = recipe.id

    derive_count_metadata(factory, csv_path=csv_path)
    index = load_index(factory, csv_path=csv_path)
    result = B.build_basket(index, [B.Selection(rid)])
    line = result.lines[0]
    assert line.need_qty == pytest.approx(4)
    assert line.cover.packs == 2
    assert line.cover.capacity_qty == pytest.approx(4)
    assert line.cover.leftover_qty == pytest.approx(0)


def test_count_fractional_demands_snap_before_ceiling(factory, tmp_path):
    key, sid = "name:onion", "sid-onion"
    csv_path = write_freq_csv(tmp_path / "freq.csv", [(key, sid, "Onion")])
    third = 0.3333333432674408
    with factory() as s:
        seed_candidates(
            s,
            key,
            "Onion",
            [{
                "sku": "onion", "name": "Ocado Onion", "price": 0.5,
                "pack_raw": "1 per pack", "pack_value": 1, "pack_unit": "each",
            }],
        )
        service.save_decision(
            s,
            gather_candidates(s, key),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="onion", rank=1)],
            ),
        )
        recipe = Recipe(
            source="hellofresh", source_id="onion", url="", name="Onion Trio", curated=1,
            base_yield=2,
            ingredients=[
                RecipeIngredient(name="Onion", source_ingredient_id=sid, amount=third, unit="unit(s)", amount_g=36.666667),
                RecipeIngredient(name="Onion", source_ingredient_id=sid, amount=third, unit="unit(s)", amount_g=36.666667),
                RecipeIngredient(name="Onion", source_ingredient_id=sid, amount=third, unit="unit(s)", amount_g=36.666667),
            ],
        )
        s.add(recipe)
        s.commit()
        rid = recipe.id

    derive_count_metadata(factory, csv_path=csv_path)
    index = load_index(factory, csv_path=csv_path)
    result = B.build_basket(index, [B.Selection(rid)])
    line = result.lines[0]
    assert line.need_qty == pytest.approx(1)
    assert line.cover.packs == 1
    assert line.cover.leftover_qty == pytest.approx(0)


def test_consumed_cost_is_pack_pro_rata_not_whole_pack():
    spice = pack(40, 2.0, salvage=0.85)
    cover = B._cover_with_packs((spice,), need_g=2)
    line = B.BasketLine(key="name:paprika", name="Paprika", need_g=2, cover=cover)
    assert line.cost == pytest.approx(2.0)
    assert line.consumed_cost == pytest.approx(0.10)


def _seed_ingredient_with_units(session, key, sid, name, unit_lines):
    """One approved ingredient plus recipes stating it in the given units."""
    seed_candidates(
        session, key, name,
        [{"sku": f"{sid}-pack", "name": f"Ocado {name}", "price": 2.0,
          "pack_raw": "200g", "pack_value": 200, "pack_unit": "g"}],
    )
    service.save_decision(
        session, gather_candidates(session, key),
        service.DecisionInput(
            status="approved",
            accepted=[service.AcceptedInput(sku=f"{sid}-pack", rank=1)],
        ),
    )
    for i, (unit, amount, grams) in enumerate(unit_lines):
        session.add(Recipe(
            source="hellofresh", source_id=f"{sid}-r{i}", url="", name=f"R{i}",
            curated=1, base_yield=2,
            ingredients=[RecipeIngredient(
                name=name, source_ingredient_id=sid, amount=amount,
                unit=unit, amount_g=grams,
            )],
        ))
    session.commit()


def test_a_minority_of_count_lines_does_not_make_an_ingredient_countable(factory, tmp_path):
    """Rosemary is bought by the packet, not the sprig.

    It carried five stray "unit(s)" lines against 245 "bunch(es)" ones, and on the
    strength of those five was being covered in whole sprigs. A count claim has to
    win the vote, not merely appear.
    """
    key, sid = "name:rosemary", "sid-rosemary"
    csv_path = write_freq_csv(tmp_path / "freq.csv", [(key, sid, "Rosemary")])
    lines = [("bunch(es)", 1, 8.0)] * 9 + [("unit(s)", 1, 8.0)]
    with factory() as s:
        _seed_ingredient_with_units(s, key, sid, "Rosemary", lines)

    derive_count_metadata(factory, csv_path=csv_path)
    with factory() as s:
        row = s.scalars(
            select(IngredientMapping).where(IngredientMapping.ingredient_key == key)
        ).one()
        assert row.unit_kind == "mass"


def test_a_consistently_counted_ingredient_stays_countable_even_when_rare(factory, tmp_path):
    """Celeriac appears four times and is counted every time — that is not weak evidence."""
    key, sid = "name:celeriac", "sid-celeriac"
    csv_path = write_freq_csv(tmp_path / "freq.csv", [(key, sid, "Celeriac")])
    with factory() as s:
        _seed_ingredient_with_units(s, key, sid, "Celeriac", [("unit(s)", 1, 500.0)] * 4)

    derive_count_metadata(factory, csv_path=csv_path)
    with factory() as s:
        row = s.scalars(
            select(IngredientMapping).where(IngredientMapping.ingredient_key == key)
        ).one()
        assert row.unit_kind == "count"
        assert row.each_to_grams == pytest.approx(500.0)


def test_a_mislabelled_gram_weight_is_not_read_as_a_count(factory, tmp_path):
    """"200 unit(s)" is 200 g wearing the wrong label.

    to_grams passes such amounts straight through, so treating them as counts
    derives a per-unit weight of exactly 1 g — which is how Lamb Shank came to
    weigh a gram apiece.
    """
    key, sid = "name:lamb shank", "sid-shank"
    csv_path = write_freq_csv(tmp_path / "freq.csv", [(key, sid, "Lamb Shank")])
    with factory() as s:
        _seed_ingredient_with_units(s, key, sid, "Lamb Shank", [("unit(s)", 400, 400.0)] * 4)

    derive_count_metadata(factory, csv_path=csv_path)
    with factory() as s:
        row = s.scalars(
            select(IngredientMapping).where(IngredientMapping.ingredient_key == key)
        ).one()
        assert row.unit_kind == "mass"
        assert row.each_to_grams != 1.0


def test_score_basket_agrees_with_build_basket(factory, tmp_path):
    """The lean scoring path is only safe while it says the same thing.

    ``score_basket`` exists to skip the itemised basket, so nothing else would
    notice it drifting — the ranking would simply start ordering the library by a
    number the basket page disagrees with.
    """
    keys = [("name:pasta", "sid-pasta", "Pasta"), ("name:basil", "sid-basil", "Basil")]
    csv_path = write_freq_csv(tmp_path / "freq.csv", keys)
    with factory() as s:
        seed_candidates(s, "name:pasta", "Pasta", [
            {"sku": "p500", "name": "Pasta 500g", "price": 1.2, "pack_value": 500, "pack_unit": "g"},
        ])
        seed_candidates(s, "name:basil", "Basil", [
            {"sku": "b30", "name": "Basil 30g", "price": 0.9, "pack_value": 30, "pack_unit": "g"},
        ])
        for key, sku in (("name:pasta", "p500"), ("name:basil", "b30")):
            service.save_decision(
                s,
                gather_candidates(s, key),
                service.DecisionInput(
                    status="approved",
                    accepted=[service.AcceptedInput(sku=sku, rank=1)],
                ),
            )
        rids = []
        for i, (pasta_g, basil_g) in enumerate([(180, 10), (350, 25), (90, 5)]):
            r = Recipe(
                source="hellofresh", source_id=f"r{i}", url="", name=f"R{i}",
                curated=1, base_yield=2,
                ingredients=[
                    RecipeIngredient(name="Pasta", source_ingredient_id="sid-pasta",
                                     amount=pasta_g, unit="grams", amount_g=pasta_g),
                    RecipeIngredient(name="Basil", source_ingredient_id="sid-basil",
                                     amount=basil_g, unit="grams", amount_g=basil_g),
                ],
            )
            s.add(r)
            s.flush()
            rids.append(r.id)
        s.commit()

    index = load_index(factory, csv_path=csv_path)
    selection_sets = [
        [B.Selection(rids[0], 4)],
        [B.Selection(rids[1], 2)],
        [B.Selection(rids[0], 4), B.Selection(rids[1], 4)],
        [B.Selection(r, 4) for r in rids],
    ]
    for selections in selection_sets:
        built = B.build_basket(index, selections)
        scored = B.score_basket(index, selections)
        assert scored.score == pytest.approx(built.score)
        assert scored.cost == pytest.approx(built.cost)
        assert scored.consumed_cost == pytest.approx(built.consumed_cost)
        assert scored.gap_count == B.basket_gap_count(built)
