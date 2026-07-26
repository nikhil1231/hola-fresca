"""Hand-sourced products: things no retailer sells.

The behaviour that matters is that these stay *costed*. An ingredient with no
Ocado match must not end up free, because a free ingredient makes the planner
prefer the recipes using it.
"""
from __future__ import annotations

import pytest

from app.db.models import Recipe, RecipeIngredient
from app.mapping import manual, service
from app.mapping.candidates import gather_candidates
from app.planner import basket as B
from app.planner.index import load_index

from tests.conftest import seed_candidates
from tests.test_planner_basket import write_freq_csv

KEY = "name:thai style spice mix"
SID = "sid-thai"
NAME = "Thai Style Spice Mix"

BLEND = manual.ManualProductInput(
    name="HelloFresh Thai Style Spice Mix",
    price=3.50,
    pack_size_value=50,
    pack_size_unit="g",
    source_note="HelloFresh",
)


def test_derives_unit_price_and_assumes_it_keeps(factory):
    with factory() as s:
        product = manual.upsert_product(s, BLEND)
        s.commit()
        assert product.retailer == "manual"
        assert product.sku == "manual:hellofresh-thai-style-spice-mix"
        assert product.pack_size_raw == "50g"
        assert product.unit_price == 70.0  # £3.50 / 50 g == £70/kg
        assert product.shelf_life_days == manual.DEFAULT_SHELF_LIFE_DAYS
        assert product.in_stock == 1


def test_upsert_updates_in_place_rather_than_duplicating(factory):
    with factory() as s:
        manual.upsert_product(s, BLEND)
        s.commit()
        cheaper = manual.ManualProductInput(**{**vars(BLEND), "price": 2.75})
        product = manual.upsert_product(s, cheaper)
        s.commit()
        assert product.price == 2.75
        assert len(manual.list_products(s)) == 1


def test_rejects_a_product_with_no_pack_size(factory):
    with factory() as s:
        bad = manual.ManualProductInput(**{**vars(BLEND), "pack_size_value": 0})
        with pytest.raises(ValueError, match="pack size"):
            manual.upsert_product(s, bad)


@pytest.fixture
def seeded(factory, tmp_path):
    """The ingredient, one poor Ocado substitute, and a recipe that uses it."""
    csv_path = write_freq_csv(tmp_path / "freq.csv", [(KEY, SID, NAME)])
    with factory() as s:
        seed_candidates(s, KEY, NAME, [{
            "sku": "curry1", "name": "Generic Curry Powder", "price": 1.00,
            "pack_value": 100, "pack_unit": "g",
        }])
        service.save_decision(
            s, gather_candidates(s, KEY, name=NAME),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="curry1", rank=1, match_type="substitute")],
            ),
        )
        recipe = Recipe(
            source="hellofresh", source_id="r1", url="", name="Thai Curry", curated=1, base_yield=2,
            ingredients=[
                RecipeIngredient(name=NAME, source_ingredient_id=SID,
                                 amount=8, unit="grams", amount_g=8),
            ],
        )
        s.add(recipe)
        s.commit()
        rid = recipe.id
    return factory, csv_path, rid


def test_resolve_ranks_the_real_article_above_the_substitute(seeded):
    factory, csv_path, rid = seeded
    with factory() as s:
        mapping = manual.resolve_ingredient(s, KEY, BLEND, usage=None)
        assert mapping.status == "approved"
        assert mapping.name == NAME  # not renamed after the product or the raw key
        ranked = sorted((p for p in mapping.products if p.accepted), key=lambda p: p.rank)
        assert [p.sku for p in ranked] == ["manual:hellofresh-thai-style-spice-mix", "curry1"]


def test_basket_buys_the_manual_product_and_lists_it_separately(seeded):
    """The Ocado substitute is cheaper per gram; the real blend still wins."""
    factory, csv_path, rid = seeded
    with factory() as s:
        manual.resolve_ingredient(s, KEY, BLEND, usage=None)

    index = load_index(factory, csv_path=csv_path)
    result = B.build_basket(index, [B.Selection(rid)])
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.external is True
    assert line.cover.choices[0].pack.product_name == BLEND.name
    assert result.retailer_lines == []
    assert result.external_lines == [line]
    # Costed, not free: the whole point of doing this at all.
    assert result.cost == pytest.approx(3.50)


def test_unresolved_ingredient_would_have_been_free(seeded):
    """Contrast for the test above: with no mapping at all the recipe costs nothing."""
    factory, csv_path, rid = seeded
    with factory() as s:
        service.save_decision(
            s, gather_candidates(s, KEY, name=NAME),
            service.DecisionInput(status="no_match", accepted=[]),
        )
    index = load_index(factory, csv_path=csv_path)
    result = B.build_basket(index, [B.Selection(rid)])
    assert result.cost == 0.0
    assert result.unmapped == [NAME]


def test_delete_refuses_while_an_ingredient_still_uses_it(seeded):
    factory, csv_path, rid = seeded
    with factory() as s:
        manual.resolve_ingredient(s, KEY, BLEND, usage=None)
        with pytest.raises(ValueError, match="still used by"):
            manual.delete_product(s, manual.manual_sku(BLEND.name))


def test_delete_succeeds_once_nothing_accepts_it(seeded):
    factory, csv_path, rid = seeded
    sku = manual.manual_sku(BLEND.name)
    with factory() as s:
        manual.resolve_ingredient(s, KEY, BLEND, usage=None)
        service.save_decision(
            s, gather_candidates(s, KEY, name=NAME),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="curry1", rank=1, match_type="substitute")],
            ),
        )
        manual.delete_product(s, sku)
        assert manual.list_products(s) == []


@pytest.fixture
def client(seeded, monkeypatch):
    """API client over the seeded ingredient, so route wiring is covered too."""
    from fastapi.testclient import TestClient

    import main
    from app.api import mapping as mapping_api
    from app.api.deps import get_session

    factory, csv_path, _ = seeded
    # The API loads usage stats from DATA_DIR under the conventional filename.
    write_freq_csv(csv_path.parent / "ingredient_frequency.csv", [(KEY, SID, NAME)])
    monkeypatch.setattr("app.config.DATA_DIR", csv_path.parent)
    mapping_api._usage_stats.cache_clear()

    def override():
        with factory() as session:
            yield session

    main.app.dependency_overrides[get_session] = override
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    mapping_api._usage_stats.cache_clear()


def test_api_resolves_an_ingredient_and_tags_the_candidate_retailer(client):
    from urllib.parse import quote

    res = client.post(
        f"/api/mapping/ingredients/{quote(KEY, safe='')}/manual",
        json={"name": BLEND.name, "price": 3.5, "pack_size_value": 50, "pack_size_unit": "g"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "approved"
    manual_candidates = [c for c in body["candidates"] if c["retailer"] == "manual"]
    assert [c["name"] for c in manual_candidates] == [BLEND.name]
    assert manual_candidates[0]["accepted"] is True


def test_api_refuses_to_delete_a_product_in_use(client):
    from urllib.parse import quote

    client.post(
        f"/api/mapping/ingredients/{quote(KEY, safe='')}/manual",
        json={"name": BLEND.name, "price": 3.5, "pack_size_value": 50},
    )
    res = client.delete(f"/api/mapping/manual-products/{manual.manual_sku(BLEND.name)}")
    assert res.status_code == 409
    assert "still used by" in res.json()["detail"]


def test_api_rejects_a_product_without_a_price(client):
    res = client.post("/api/mapping/manual-products", json={"name": "No Price", "price": -1,
                                                            "pack_size_value": 10})
    assert res.status_code == 400


def test_attach_offers_an_existing_product_to_another_ingredient(seeded):
    factory, csv_path, rid = seeded
    with factory() as s:
        manual.upsert_product(s, BLEND)
        s.commit()
        manual.attach(s, KEY, manual.manual_sku(BLEND.name))
        s.commit()
        skus = {c.sku for c in gather_candidates(s, KEY).candidates}
        assert manual.manual_sku(BLEND.name) in skus
        # Idempotent: attaching twice must not duplicate the candidate.
        manual.attach(s, KEY, manual.manual_sku(BLEND.name))
        s.commit()
        hits = [c for c in gather_candidates(s, KEY).candidates
                if c.sku == manual.manual_sku(BLEND.name)]
        assert len(hits) == 1
