"""Mapping review API: list, detail overlay, save decision, bulk approve."""
from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.db.models import IngredientMapping, Recipe, RecipeCuisine, RecipeIngredient
from tests.conftest import seed_candidates

KEY = "name:chicken breast"
KEY_Q = quote(KEY, safe="")
PRODUCTS = [
    {
        "sku": "p1",
        "name": "Ocado Chicken Breast",
        "price": 3.5,
        "base_price": 4.25,
        "pack_value": 600,
        "pack_unit": "g",
    },
    {"sku": "p2", "name": "Mini Fillets", "price": 2.5, "pack_value": 300, "pack_unit": "g"},
]


@pytest.fixture
def client(factory, tmp_path, monkeypatch):
    import main
    from app.api import mapping as mapping_api
    from app.api.deps import get_session

    (tmp_path / "ingredient_frequency.csv").write_text(
        "rank,ingredient_key,source_ingredient_ids,name,line_count,metric_unit,"
        "median_metric_amount,p25_metric_amount,p75_metric_amount,common_native_amounts,name_variants\n"
        "1,name:chicken breast,sid1,Chicken Breast,500,g,450,400,500,,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path)
    mapping_api._usage_stats.cache_clear()

    def override():
        with factory() as session:
            yield session

    main.app.dependency_overrides[get_session] = override
    with factory() as s:
        seed_candidates(s, KEY, "Chicken Breast", PRODUCTS, line_count=500)
        recipe = Recipe(
            source="hellofresh",
            source_id="hf-chicken",
            url="https://example.com/chicken",
            name="Chicken Dinner",
            headline="with greens",
            curated=1,
            is_complete=1,
            image_path="/recipes/chicken.jpg",
            avg_rating=4.7,
            ratings_count=1200,
        )
        recipe.cuisines = [RecipeCuisine(name="British")]
        recipe.ingredients = [
            RecipeIngredient(
                name="Chicken Breast",
                source_ingredient_id="sid1",
                image_path="/ingredients/chicken.jpg",
            )
        ]
        s.add(recipe)
        s.commit()
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    mapping_api._usage_stats.cache_clear()


def test_detail_before_any_decision_lists_candidates(client):
    r = client.get(f"/api/mapping/ingredients/{KEY_Q}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] is None
    assert body["name"] == "Chicken Breast"
    assert len(body["candidates"]) == 2
    assert body["usage"]["median"] == 450
    assert body["ingredient_icon_url"].startswith("https://img.hellofresh.com/")
    assert body["example_recipes"][0]["name"] == "Chicken Dinner"
    assert body["candidates"][0]["base_price"] == 4.25


def test_detail_uses_frequency_name_when_mapping_name_is_stale(client, factory):
    with factory() as s:
        s.add(
            IngredientMapping(
                retailer="ocado",
                ingredient_key=KEY,
                name="Stale Search Term",
                line_count=500,
                status="needs_review",
            )
        )
        s.commit()

    r = client.get(f"/api/mapping/ingredients/{KEY_Q}")
    assert r.status_code == 200
    assert r.json()["name"] == "Chicken Breast"


def test_unknown_ingredient_returns_404(client):
    r = client.get("/api/mapping/ingredients/name:does-not-exist")
    assert r.status_code == 404


def test_save_decision_then_list_and_detail(client):
    save = client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={
            "status": "approved",
            "accepted": [{"sku": "p2", "rank": 1, "match_type": "exact", "reason": "value"}],
            "each_to_grams": None,
            "needs_substitution": False,
            "reviewer_notes": "ok",
        },
    )
    assert save.status_code == 200
    assert save.json()["status"] == "approved"
    accepted = [c for c in save.json()["candidates"] if c["accepted"]]
    assert [c["sku"] for c in accepted] == ["p2"]

    listing = client.get("/api/mapping/ingredients").json()
    assert listing["counts"] == {"approved": 1}
    assert listing["items"][0]["ingredient_key"] == KEY
    assert listing["items"][0]["num_accepted"] == 1




def test_list_endpoint_paginates_searches_and_reports_total(client, factory):
    with factory() as s:
        seed_candidates(
            s,
            "name:beef mince",
            "Beef Mince",
            [{"sku": "b1", "name": "Ocado Beef Mince", "price": 4.0}],
            line_count=100,
        )

    client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={"status": "proposed", "accepted": [{"sku": "p1", "rank": 1}]},
    )
    client.post(
        "/api/mapping/ingredients/name%3Abeef%20mince",
        json={"status": "approved", "accepted": [{"sku": "b1", "rank": 1}]},
    )

    page = client.get(
        "/api/mapping/ingredients", params={"page_size": 1, "page": 1, "q": "mince"}
    ).json()
    assert page["total"] == 1
    assert page["page"] == 1
    assert page["page_size"] == 1
    assert page["has_more"] is False
    assert page["items"][0]["ingredient_key"] == "name:beef mince"


def test_alias_options_are_lightweight_and_exclude_current(client, factory):
    with factory() as s:
        seed_candidates(
            s,
            "name:beef mince",
            "Beef Mince",
            [{"sku": "b1", "name": "Ocado Beef Mince", "price": 4.0}],
            line_count=100,
        )
    client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={"status": "approved", "accepted": [{"sku": "p1", "rank": 1}]},
    )
    client.post(
        "/api/mapping/ingredients/name%3Abeef%20mince",
        json={"status": "approved", "accepted": [{"sku": "b1", "rank": 1}]},
    )

    body = client.get("/api/mapping/alias-options", params={"exclude": KEY}).json()
    keys = {i["ingredient_key"] for i in body["items"]}
    assert KEY not in keys
    assert "name:beef mince" in keys


def test_unknown_accepted_sku_rejected_by_api(client):
    r = client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={"status": "approved", "accepted": [{"sku": "ghost", "rank": 1}]},
    )
    assert r.status_code == 400
    assert "unknown accepted sku" in r.json()["detail"]


def test_empty_non_pantry_approval_rejected_by_api(client):
    r = client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={"status": "approved", "accepted": []},
    )
    assert r.status_code == 400
    assert "at least one accepted product" in r.json()["detail"]


def test_invalid_status_rejected(client):
    r = client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={"status": "banana", "accepted": []},
    )
    assert r.status_code == 400


def test_bulk_approve_endpoint(client):
    client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={"status": "proposed", "accepted": [{"sku": "p1", "rank": 1, "match_type": "exact"}]},
    )
    r = client.post("/api/mapping/bulk-approve", json={"keys": [KEY]})
    assert r.status_code == 200 and r.json()["approved"] == 1
    assert client.get("/api/mapping/ingredients").json()["counts"] == {"approved": 1}


def test_stats_reports_coverage_and_remaining(client):
    r = client.get("/api/mapping/stats")
    assert r.status_code == 200
    body = r.json()
    # One curated recipe is seeded, and its only ingredient is unmapped here.
    assert set(body) >= {
        "recipes_total", "recipes_priceable", "recipes_pct",
        "lines_total", "lines_resolved", "lines_pct",
        "distinct_keys", "resolved_keys", "mappings_total", "approved",
        "remaining_to_add",
    }
    assert body["lines_pct"] == 0.0
    assert body["recipes_total"] == 1
    assert body["recipes_priceable"] == 0
    assert body["recipes_pct"] == 0.0
    assert body["remaining_to_add"] == 0  # the one CSV row already has candidates


def test_stats_headline_recipe_becomes_priceable_once_its_last_line_maps(client):
    """The headline is per recipe: mapping the last gap flips it in one step."""
    before = client.get("/api/mapping/stats").json()
    assert before["recipes_priceable"] == 0

    client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={"status": "approved", "accepted": [{"sku": "p1", "rank": 1}]},
    )

    after = client.get("/api/mapping/stats").json()
    assert after["recipes_priceable"] == 1
    assert after["recipes_pct"] == 100.0
    assert after["lines_pct"] == 100.0


def test_stats_counts_approved_mappings(client):
    client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={"status": "approved", "accepted": [{"sku": "p1", "rank": 1}]},
    )
    body = client.get("/api/mapping/stats").json()
    assert body["approved"] == 1
    assert body["mappings_total"] == 1
