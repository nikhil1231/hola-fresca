"""Choosing a shop: the registry, the per-user setting, and what it scopes."""
from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import retailers
from app.db.models import PlanSettings
from app.mapping import service
from app.mapping.candidates import gather_candidates
from app.planner.index import Pack
from tests.conftest import seed_candidates, user_id

KEY = "name:chicken breast"
KEY_Q = quote(KEY, safe="")

OCADO_PRODUCTS = [
    {"sku": "oc-1", "name": "Ocado Chicken Breast 600g", "price": 3.5,
     "pack_value": 600, "pack_unit": "g"},
]
SAINSBURYS_PRODUCTS = [
    {"sku": "sa-1", "name": "Sainsbury's British Chicken Breast 600g", "price": 4.25,
     "pack_value": 600, "pack_unit": "g"},
]


# --- the registry -----------------------------------------------------------


def test_manual_is_not_a_shop_you_can_select():
    # It is a value Product.retailer takes, but "shop at manual" is a bug.
    assert retailers.MANUAL_RETAILER not in retailers.RETAILER_IDS
    with pytest.raises(KeyError):
        retailers.get(retailers.MANUAL_RETAILER)


def test_ocado_is_shoppable_and_sainsburys_is_only_catalogued():
    # The capability, not the name, is what the UI branches on.
    assert retailers.get("ocado").shoppable
    assert retailers.get("sainsburys").catalogued
    assert not retailers.get("sainsburys").shoppable


def test_resolve_degrades_an_unknown_value_to_the_default():
    # A retailer retired after someone selected it must not 500 their basket.
    assert retailers.resolve("waitrose") == retailers.DEFAULT_RETAILER
    assert retailers.resolve(None) == retailers.DEFAULT_RETAILER
    assert retailers.resolve("sainsburys") == "sainsburys"


def test_label_is_tolerant_of_ids_that_are_not_shops():
    assert retailers.label("sainsburys") == "Sainsbury's"
    assert retailers.label(retailers.MANUAL_RETAILER) == "Bought by hand"


# --- Pack.external ----------------------------------------------------------


def _pack(retailer: str, shop: str) -> Pack:
    return Pack(
        sku="x", product_name="x", capacity_g=100, price=1.0, salvage=0.0,
        rank=1, match_type="exact", retailer=retailer, shop=shop,
    )


def test_a_pack_is_external_only_to_a_shop_that_does_not_sell_it():
    # The bug this guards: "external" used to mean "not Ocado", which made every
    # Sainsbury's pack look like something you go out and buy by hand.
    assert not _pack("sainsburys", "sainsburys").external
    assert _pack("sainsburys", "ocado").external
    assert not _pack("ocado", "ocado").external


def test_manual_products_stay_external_at_every_shop():
    for shop in retailers.RETAILER_IDS:
        assert _pack(retailers.MANUAL_RETAILER, shop).external


# --- the API ----------------------------------------------------------------


@pytest.fixture
def client(factory, tmp_path, monkeypatch):
    import main
    from app.api import mapping as mapping_api
    from app.api.deps import get_planner_csv_path, get_session, get_session_factory

    csv_path = tmp_path / "ingredient_frequency.csv"
    csv_path.write_text(
        "rank,ingredient_key,source_ingredient_ids,name,line_count,metric_unit,"
        "median_metric_amount,p25_metric_amount,p75_metric_amount,common_native_amounts,name_variants\n"
        f"1,{KEY},sid1,Chicken Breast,500,g,450,400,500,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path)
    mapping_api._usage_stats.cache_clear()

    def override():
        with factory() as session:
            yield session

    main.app.dependency_overrides[get_session] = override
    # The planner index owns its own session lifecycle, so overriding get_session
    # alone would leave every priced read pointed at the real database.
    main.app.dependency_overrides[get_session_factory] = lambda: factory
    main.app.dependency_overrides[get_planner_csv_path] = lambda: csv_path
    with factory() as s:
        seed_candidates(s, KEY, "Chicken Breast", OCADO_PRODUCTS, retailer="ocado")
        seed_candidates(s, KEY, "Chicken Breast", SAINSBURYS_PRODUCTS, retailer="sainsburys")
        # One approved mapping per shop — they are separate rows by design.
        # Written through save_decision rather than by hand so the link back to
        # the product row (which is what gives the pack a price) is made the same
        # way the review UI makes it.
        for retailer, sku in (("ocado", "oc-1"), ("sainsburys", "sa-1")):
            ic = gather_candidates(s, KEY, name="Chicken Breast", retailer=retailer)
            service.save_decision(
                s,
                ic,
                service.DecisionInput(
                    status="approved",
                    accepted=[service.AcceptedInput(sku=sku, rank=1, match_type="exact")],
                ),
                retailer,
            )
        s.commit()

    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    mapping_api._usage_stats.cache_clear()


def test_lists_every_shop_and_the_active_one(client):
    body = client.get("/api/retailers").json()
    assert body["active"] == "ocado"
    assert [item["id"] for item in body["items"]] == ["ocado", "sainsburys"]
    assert [item["shoppable"] for item in body["items"]] == [True, False]


def test_selecting_a_shop_persists_it(client):
    assert client.put("/api/retailers", json={"retailer": "sainsburys"}).json()["active"] == (
        "sainsburys"
    )
    assert client.get("/api/retailers").json()["active"] == "sainsburys"


def test_selecting_an_unknown_shop_is_rejected(client):
    assert client.put("/api/retailers", json={"retailer": "waitrose"}).status_code == 400
    assert client.get("/api/retailers").json()["active"] == "ocado"


def test_reading_the_active_shop_does_not_create_a_settings_row(client, factory):
    # A page load is not a change. plan_settings is created lazily by whatever
    # first writes to it, and a GET that materialised a row would defeat that.
    client.get("/api/retailers")
    with factory() as s:
        assert s.query(PlanSettings).count() == 0


def test_selecting_a_shop_creates_the_settings_row_with_defaults_intact(client, factory):
    client.put("/api/retailers", json={"retailer": "sainsburys"})
    with factory() as s:
        row = s.query(PlanSettings).one()
        assert row.retailer == "sainsburys"
        # It borrowed the schedule's defaults rather than inventing any.
        assert row.cadence_weeks == 1 and not row.paused


def test_an_unknown_stored_value_falls_back_rather_than_failing(client, factory):
    # e.g. a retailer removed from the registry after someone selected it.
    with factory() as s:
        s.add(
            PlanSettings(
                user_id=user_id(s), anchor_week_start="2026-08-17", retailer="waitrose"
            )
        )
        s.commit()
    assert client.get("/api/retailers").json()["active"] == retailers.DEFAULT_RETAILER


# --- what the choice actually scopes ----------------------------------------


def test_the_mapping_queue_shows_the_active_shop_only(client):
    ocado = client.get(f"/api/mapping/ingredients/{KEY_Q}").json()
    assert [c["sku"] for c in ocado["candidates"]] == ["oc-1"]

    client.put("/api/retailers", json={"retailer": "sainsburys"})
    sainsburys = client.get(f"/api/mapping/ingredients/{KEY_Q}").json()
    assert [c["sku"] for c in sainsburys["candidates"]] == ["sa-1"]


def test_switching_shop_reprices_the_same_ingredient(client, factory):
    """The point of the whole feature: one week, two prices.

    Both mappings are approved and cover the same ingredient, so the only thing
    that changes between these two reads is which catalogue the packs came from.
    """
    from app.db.models import Recipe, RecipeIngredient

    with factory() as s:
        recipe = Recipe(
            source="hellofresh", source_id="hf-1", url="https://example.com/1",
            name="Chicken dinner", base_yield=2, curated=1, is_complete=1,
        )
        s.add(recipe)
        s.flush()
        s.add(
            RecipeIngredient(
                recipe_id=recipe.id, source_ingredient_id="sid1", name="Chicken Breast",
                amount=600, unit="g", amount_g=600, canonical_unit="g", position=1,
            )
        )
        s.commit()
        recipe_id = recipe.id

    payload = {"selections": [{"recipe_id": recipe_id, "portions": 2}]}

    ocado = client.post("/api/planner/basket", json=payload).json()
    client.put("/api/retailers", json={"retailer": "sainsburys"})
    sainsburys = client.post("/api/planner/basket", json=payload).json()

    assert ocado["cost"] == pytest.approx(3.5)
    assert sainsburys["cost"] == pytest.approx(4.25)
    # And neither basket treats the other shop's pack as something bought by hand.
    for body in (ocado, sainsburys):
        assert not any(line["external"] for line in body["lines"])
