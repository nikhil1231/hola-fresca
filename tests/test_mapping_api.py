"""Mapping review API: list, detail overlay, save decision, bulk approve."""
from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.db.models import Recipe, RecipeCuisine, RecipeIngredient
from tests.conftest import seed_candidates

KEY = "name:chicken breast"
KEY_Q = quote(KEY, safe="")
PRODUCTS = [
    {"sku": "p1", "name": "Ocado Chicken Breast", "price": 3.5, "pack_value": 600, "pack_unit": "g"},
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
    assert len(body["candidates"]) == 2
    assert body["usage"]["median"] == 450
    assert body["ingredient_icon_url"].startswith("https://img.hellofresh.com/")
    assert body["example_recipes"][0]["name"] == "Chicken Dinner"


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
    # No curated recipes seeded, so line coverage is zero but the shape is right.
    assert set(body) >= {
        "lines_total", "lines_resolved", "lines_pct",
        "distinct_keys", "resolved_keys", "mappings_total", "approved",
        "remaining_to_add",
    }
    assert body["lines_pct"] == 0.0
    assert body["remaining_to_add"] == 0  # the one CSV row already has candidates


def test_stats_counts_approved_mappings(client):
    client.post(
        f"/api/mapping/ingredients/{KEY_Q}",
        json={"status": "approved", "accepted": [{"sku": "p1", "rank": 1}]},
    )
    body = client.get("/api/mapping/stats").json()
    assert body["approved"] == 1
    assert body["mappings_total"] == 1


# --- specialist catalogue endpoints ----------------------------------------

CAT_KEY = "name:chermoula spice mix"
CAT_KEY_Q = quote(CAT_KEY, safe="")


def _catalogue(tmp_path, captured_at="2026-07-28"):
    """A two-product Seasoned Pioneers snapshot on disk."""
    import json

    def product(woo_id, name, price, size):
        return {
            "id": woo_id,
            "name": name,
            "sku": "",
            "type": "simple",
            "permalink": f"https://www.seasonedpioneers.com/x/{woo_id}/",
            "prices": {"price": price, "currency_minor_unit": 2, "currency_code": "GBP"},
            "categories": [{"slug": "moroccan", "name": "Moroccan Spices"}],
            "images": [],
            "average_rating": "4.9",
            "review_count": 17,
            "is_in_stock": True,
            "size_raw": size,
        }

    path = tmp_path / "sp_catalogue.json"
    path.write_text(
        json.dumps({
            "captured_at": captured_at,
            "products": [
                product(4996, "Chermoula Spice Mix", "350", "35g"),
                product(5001, "Ras el Hanout", "350", "33g"),
            ],
        }),
        encoding="utf-8",
    )
    return path


def _seed_spice(factory, tmp_path, monkeypatch, *, native="1 sachet(s) (244)"):
    """Sync the catalogue and put a spice ingredient into the review queue.

    The shared fixture's only ingredient is Chicken Breast, which arrives by
    weight — deliberately not a seasoning, so it exercises the guard rather than
    the match.
    """
    from app.api import mapping as mapping_api
    from app.db.models import IngredientMapping
    from app.scraper.products import catalogue

    path = _catalogue(tmp_path)
    monkeypatch.setattr(
        "app.scraper.products.seasoned_pioneers.CATALOGUE_PATH", path
    )
    catalogue.sync(factory, path=path, cache_raw=False)

    csv_path = tmp_path / "ingredient_frequency.csv"
    with csv_path.open("a", encoding="utf-8") as fh:
        fh.write(
            f"2,{CAT_KEY},sid2,Chermoula Spice Mix,318,g,8,8,8,{native},\n"
        )
    mapping_api._usage_stats.cache_clear()

    with factory() as s:
        s.add(
            IngredientMapping(
                retailer="ocado",
                ingredient_key=CAT_KEY,
                name="Chermoula Spice Mix",
                line_count=318,
                status="proposed",
            )
        )
        s.commit()
    return path


def test_catalogue_status_reports_nothing_before_a_sync(client):
    body = client.get("/api/mapping/catalogue/status").json()
    assert body["products"] == 0
    assert body["retailer"] == "seasoned_pioneers"


def test_catalogue_status_reports_the_synced_snapshot(client, factory, tmp_path, monkeypatch):
    _seed_spice(factory, tmp_path, monkeypatch)

    body = client.get("/api/mapping/catalogue/status").json()
    assert body["products"] == 2
    assert body["in_stock"] == 2
    assert body["captured_at"] == "2026-07-28"


def test_catalogue_preview_scores_without_attaching(client, factory, tmp_path, monkeypatch):
    _seed_spice(factory, tmp_path, monkeypatch)

    body = client.get(f"/api/mapping/ingredients/{CAT_KEY_Q}/catalogue").json()
    assert [i["product_name"] for i in body["items"]] == ["Chermoula Spice Mix"]
    assert body["items"][0]["score"] == 1.0
    assert body["items"][0]["pack_size_raw"] == "35g"

    # Preview attaches nothing, so the candidate pool is untouched.
    detail = client.get(f"/api/mapping/ingredients/{CAT_KEY_Q}").json()
    assert detail["candidates"] == []


def test_catalogue_attach_adds_candidates_to_the_ingredient(client, factory, tmp_path, monkeypatch):
    _seed_spice(factory, tmp_path, monkeypatch)

    body = client.post(f"/api/mapping/ingredients/{CAT_KEY_Q}/catalogue").json()
    candidates = [c for c in body["candidates"] if c["retailer"] == "seasoned_pioneers"]
    assert [c["name"] for c in candidates] == ["Chermoula Spice Mix"]
    assert candidates[0]["pack_size_value"] == 35.0
    assert candidates[0]["price"] == 3.5


def test_catalogue_attach_accepts_alternative_wording(client, factory, tmp_path, monkeypatch):
    """The q override is the reviewer's escape hatch when the name does not match."""
    _seed_spice(factory, tmp_path, monkeypatch)

    body = client.post(
        f"/api/mapping/ingredients/{CAT_KEY_Q}/catalogue", params={"q": "Ras el Hanout"}
    ).json()
    names = {c["name"] for c in body["candidates"] if c["retailer"] == "seasoned_pioneers"}
    assert "Ras el Hanout" in names


def test_catalogue_bulk_attach_matches_the_queue(client, factory, tmp_path, monkeypatch):
    _seed_spice(factory, tmp_path, monkeypatch)

    body = client.post("/api/mapping/catalogue/attach", json={}).json()
    assert body["considered"] == 1
    assert body["ingredients_matched"] == 1
    assert body["hits_added"] == 1

    detail = client.get(f"/api/mapping/ingredients/{CAT_KEY_Q}").json()
    assert [c["name"] for c in detail["candidates"]] == ["Chermoula Spice Mix"]


def test_catalogue_bulk_attach_skips_ingredients_not_shipped_as_seasonings(
    client, factory, tmp_path, monkeypatch
):
    """Fresh produce shares names with dried spice; how it arrives settles it."""
    _seed_spice(factory, tmp_path, monkeypatch, native="1 unit(s) (244)")

    body = client.post("/api/mapping/catalogue/attach", json={}).json()
    assert body["considered"] == 0
    assert body["hits_added"] == 0
    assert body["skipped_not_seasoning"] == 1

    # ...and turning the guard off reaches it after all.
    body = client.post(
        "/api/mapping/catalogue/attach", json={"seasonings_only": False}
    ).json()
    assert body["hits_added"] == 1
