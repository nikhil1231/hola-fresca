"""Recipe browse API tests.

A temporary SQLite DB is seeded with a handful of hand-built recipes covering the
filter dimensions, and the ``get_session`` dependency is overridden to point at
it. No network and no dependency on the real catalogue.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_planner_csv_path, get_session, get_session_factory
from app.db.models import (
    Recipe,
    RecipeAllergen,
    RecipeCuisine,
    RecipeIngredient,
    RecipeNutrition,
    RecipeStep,
    RecipeTag,
)
from app.db.session import init_db, make_engine, make_session_factory
from app.mapping import service
from app.mapping.candidates import gather_candidates
from main import app
from tests.conftest import seed_candidates
from tests.test_planner_basket import write_freq_csv


def _make_recipe(**overrides) -> Recipe:
    defaults = dict(
        source="hellofresh",
        url="https://example.com/r",
        name="Recipe",
        curated=1,
        is_complete=1,
        image_path="/image/x.jpg",
    )
    defaults.update(overrides)
    return Recipe(**defaults)


@pytest.fixture
def client(tmp_path):
    engine = make_engine(tmp_path / "api.db")
    init_db(engine)
    factory = make_session_factory(engine)
    csv_path = write_freq_csv(
        tmp_path / "ingredient_frequency.csv",
        [
            ("name:pasta", "sid-pasta", "Pasta"),
            ("name:lentils", "sid-lentils", "Lentils"),
            ("name:chicken", "sid-chicken", "Chicken Breast"),
            ("name:chicken stock paste", "sid-chicken-stock", "Chicken Stock Paste"),
            ("name:tortillas", "sid-tortillas", "Taco Tortillas"),
            ("name:macaroni", "sid-macaroni", "Macaroni"),
        ],
    )

    with factory() as s:
        seed_candidates(
            s,
            "name:pasta",
            "Pasta",
            [{"sku": "pasta", "name": "Pasta 500g", "price": 1.0, "pack_value": 500, "pack_unit": "g"}],
        )
        seed_candidates(
            s,
            "name:lentils",
            "Lentils",
            [{"sku": "lentils", "name": "Lentils 400g", "price": 1.0, "pack_value": 400, "pack_unit": "g"}],
        )
        seed_candidates(
            s,
            "name:chicken",
            "Chicken Breast",
            [{"sku": "chicken", "name": "Chicken 500g", "price": 5.0, "pack_value": 500, "pack_unit": "g"}],
        )
        seed_candidates(
            s,
            "name:chicken stock paste",
            "Chicken Stock Paste",
            [{"sku": "stock", "name": "Chicken Stock Paste", "price": 1.0, "pack_value": 100, "pack_unit": "g"}],
        )
        seed_candidates(
            s,
            "name:macaroni",
            "Macaroni",
            [{"sku": "mac", "name": "Macaroni 500g", "price": 1.0, "pack_value": 500, "pack_unit": "g"}],
        )
        for key, sku in (
            ("name:pasta", "pasta"),
            ("name:lentils", "lentils"),
            ("name:chicken", "chicken"),
            ("name:chicken stock paste", "stock"),
            ("name:macaroni", "mac"),
        ):
            service.save_decision(
                s,
                gather_candidates(s, key),
                service.DecisionInput(
                    status="approved",
                    accepted=[service.AcceptedInput(sku=sku, rank=1)],
                ),
            )

        italian = _make_recipe(
            source_id="a", name="Creamy Veggie Pasta", protein_g=50, energy_kcal=600,
            total_time_min=20, difficulty=1, avg_rating=4.5, ratings_count=900,
            protein_energy_ratio=8.3, is_vegetarian=1, is_pescatarian=1,
        )
        italian.cuisines = [RecipeCuisine(name="Italian")]
        italian.tags = [RecipeTag(name="X", type="seo")]  # no attribute chip
        italian.allergens = [RecipeAllergen(name="Milk")]
        italian.ingredients = [
            RecipeIngredient(name="Pasta", source_ingredient_id="sid-pasta", position=2, amount=180, unit="grams", amount_g=180, canonical_unit="g"),
            RecipeIngredient(name="Lentils", source_ingredient_id="sid-lentils", position=1, amount=1, unit="carton(s)", amount_g=250, canonical_unit="g"),
            RecipeIngredient(name="Chicken Stock Paste", source_ingredient_id="sid-chicken-stock", position=3, amount=0, unit="sachet(s)", amount_g=0),
        ]
        italian.steps = [
            RecipeStep(
                index=1,
                instructions_text="Boil pasta",
                image_path="/steps/boil-pasta.jpg",
            )
        ]
        italian.nutrition = [RecipeNutrition(name="Protein", amount=50, unit="g")]

        mexican = _make_recipe(
            source_id="b", name="Spicy Chicken Tacos", protein_g=30, energy_kcal=800,
            total_time_min=40, difficulty=2, avg_rating=4.0, ratings_count=1500,
            protein_energy_ratio=3.8,
        )
        mexican.cuisines = [RecipeCuisine(name="Mexican")]
        mexican.tags = [RecipeTag(name="HP", type="high-protein")]
        mexican.allergens = [RecipeAllergen(name="Cereals containing gluten")]
        mexican.ingredients = [
            RecipeIngredient(name="Chicken Breast", source_ingredient_id="sid-chicken", amount=250, unit="grams", amount_g=250),
            RecipeIngredient(name="Taco Tortillas", source_ingredient_id="sid-tortillas", amount=6, unit="unit(s)"),
        ]

        speedy = _make_recipe(
            source_id="d", name="Speedy Cajun Style Chicken Macaroni", protein_g=25,
            energy_kcal=700, total_time_min=30, difficulty=1, avg_rating=4.2,
            ratings_count=700, protein_energy_ratio=3.5,
        )
        speedy.allergens = [RecipeAllergen(name="Milk")]
        speedy.ingredients = [
            RecipeIngredient(name="Chicken Breast", source_ingredient_id="sid-chicken", amount=200, unit="grams", amount_g=200),
            RecipeIngredient(name="Macaroni", source_ingredient_id="sid-macaroni", amount=200, unit="grams", amount_g=200),
        ]

        uncurated = _make_recipe(source_id="c", name="Hidden", curated=0)

        s.add_all([italian, mexican, speedy, uncurated])
        s.commit()

    def _override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_planner_csv_path] = lambda: csv_path
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_returns_only_curated(client):
    data = client.get("/api/recipes").json()
    assert data["total"] == 3
    names = {i["name"] for i in data["items"]}
    assert "Hidden" not in names


def test_hide_recipe_removes_it_from_active_library(client):
    rid = client.get("/api/recipes", params={"q": "taco"}).json()["items"][0]["id"]

    response = client.post(f"/api/recipes/{rid}/hide")

    assert response.status_code == 200
    assert response.json() == {"id": rid, "manually_excluded": True}
    data = client.get("/api/recipes").json()
    assert data["total"] == 2
    assert {item["name"] for item in data["items"]} == {
        "Creamy Veggie Pasta",
        "Speedy Cajun Style Chicken Macaroni",
    }
    assert client.get(f"/api/recipes/{rid}").status_code == 404
    assert client.post("/api/planner/basket", json={"selections": [{"recipe_id": rid, "portions": 4}]}).status_code == 400


def test_hide_rejects_unknown_or_unavailable_recipes(client):
    assert client.post("/api/recipes/4/hide").status_code == 404
    assert client.post("/api/recipes/999/hide").status_code == 404


def test_card_shows_derived_diet_chip(client):
    data = client.get("/api/recipes", params={"cuisine": "Italian"}).json()
    card = data["items"][0]
    # Derived vegetarian chip; the "seo" tag is dropped.
    assert card["tags"] == ["Vegetarian"]
    assert card["cuisines"] == ["Italian"]
    assert card["protein_energy_ratio"] == 8.3


def test_filter_by_cuisine(client):
    data = client.get("/api/recipes", params={"cuisine": "Italian"}).json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "Creamy Veggie Pasta"


def test_filter_by_diet_column(client):
    assert client.get("/api/recipes", params={"diet": "vegetarian"}).json()["total"] == 1
    # Pescatarian includes the vegetarian dish.
    assert client.get("/api/recipes", params={"diet": "pescatarian"}).json()["total"] == 1


def test_filter_min_protein_ratio(client):
    # Only the Italian (8.3) clears 5.0.
    data = client.get("/api/recipes", params={"min_protein_ratio": 5.0}).json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "Creamy Veggie Pasta"


def test_sort_protein_ratio(client):
    items = client.get("/api/recipes", params={"sort": "protein_ratio"}).json()["items"]
    assert [i["protein_energy_ratio"] for i in items] == [8.3, 3.8, 3.5]


def test_filter_min_protein_and_max_time(client):
    assert client.get("/api/recipes", params={"min_protein": 40}).json()["total"] == 1
    assert client.get("/api/recipes", params={"max_time": 25}).json()["total"] == 1


def test_exclude_allergen(client):
    data = client.get("/api/recipes", params={"exclude": "Milk"}).json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "Spicy Chicken Tacos"


def test_protein_include_filter(client):
    # Both chicken dishes have chicken as a main ingredient.
    data = client.get("/api/recipes", params={"protein": "chicken"}).json()
    assert data["total"] == 2
    assert {item["name"] for item in data["items"]} == {
        "Spicy Chicken Tacos",
        "Speedy Cajun Style Chicken Macaroni",
    }


def test_exclude_ingredient(client):
    # Excluding chicken is broad and also catches stock paste.
    data = client.get("/api/recipes", params={"exclude": "chicken"}).json()
    assert data["total"] == 0


def test_exclude_unmapped_recipes(client):
    data = client.get("/api/recipes", params={"exclude": "unmapped"}).json()
    assert data["total"] == 2
    assert {item["name"] for item in data["items"]} == {
        "Creamy Veggie Pasta",
        "Speedy Cajun Style Chicken Macaroni",
    }


def test_sort_protein_high(client):
    items = client.get("/api/recipes", params={"sort": "protein_high"}).json()["items"]
    assert [i["protein_g"] for i in items] == [50, 30, 25]


def test_sort_by_intrinsic_price(client):
    low = client.get("/api/recipes", params={"sort": "price_low"}).json()["items"]
    high = client.get("/api/recipes", params={"sort": "price_high"}).json()["items"]
    assert [i["name"] for i in low] == [
        "Creamy Veggie Pasta",
        "Spicy Chicken Tacos",
        "Speedy Cajun Style Chicken Macaroni",
    ]
    assert [i["name"] for i in high] == [
        "Speedy Cajun Style Chicken Macaroni",
        "Spicy Chicken Tacos",
        "Creamy Veggie Pasta",
    ]
    assert low[0]["intrinsic_score"] < low[1]["intrinsic_score"]


def test_search_query(client):
    data = client.get("/api/recipes", params={"q": "taco"}).json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "Spicy Chicken Tacos"


def test_search_query_matches_split_partial_tokens(client):
    data = client.get("/api/recipes", params={"q": "speedy mac"}).json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "Speedy Cajun Style Chicken Macaroni"


def test_search_query_can_return_no_matches(client):
    data = client.get("/api/recipes", params={"q": "banana pudding"}).json()
    assert data["total"] == 0
    assert data["items"] == []


def test_pagination(client):
    page1 = client.get("/api/recipes", params={"page_size": 1, "page": 1}).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 1
    assert page1["has_more"] is True
    page2 = client.get("/api/recipes", params={"page_size": 1, "page": 2}).json()
    assert page2["has_more"] is True
    page3 = client.get("/api/recipes", params={"page_size": 1, "page": 3}).json()
    assert page3["has_more"] is False


def test_detail_shape_and_image(client):
    rid = client.get("/api/recipes", params={"cuisine": "Italian"}).json()["items"][0]["id"]
    detail = client.get(f"/api/recipes/{rid}").json()
    assert detail["name"] == "Creamy Veggie Pasta"
    assert [i["name"] for i in detail["ingredients"]] == ["Lentils", "Pasta"]
    # Canonical grams flow through, incl. the count->grams conversion.
    lentils = next(i for i in detail["ingredients"] if i["name"] == "Lentils")
    assert lentils["amount_g"] == 250
    assert lentils["canonical_unit"] == "g"
    assert detail["steps"][0]["text"] == "Boil pasta"
    assert detail["steps"][0]["image_url"].endswith("/steps/boil-pasta.jpg")
    assert all(not i["unmapped"] for i in detail["ingredients"])
    assert detail["image_url"].startswith("https://img.hellofresh.com/")
    assert "w_1200" in detail["image_url"]


def test_detail_404_for_uncurated(client):
    hidden = client.get("/api/recipes/4", params={})
    # id 4 is the uncurated recipe; must be hidden.
    assert hidden.status_code == 404


def test_detail_flags_unmapped_ingredients(client):
    rid = client.get("/api/recipes", params={"q": "taco"}).json()["items"][0]["id"]
    detail = client.get(f"/api/recipes/{rid}").json()
    unmapped = [i["name"] for i in detail["ingredients"] if i["unmapped"]]
    assert unmapped == ["Taco Tortillas"]


def test_facets(client):
    f = client.get("/api/facets").json()
    cuisine_labels = {c["label"] for c in f["cuisines"]}
    # Threshold is 20, and our seed has 1 each, so cuisines may be empty here;
    # the endpoint must still return the full structure.
    assert "diets" in f and "attributes" in f and "proteins" in f and "excludes" in f
    # Excludes combine allergens (e.g. Milk) and ingredient groups (e.g. chicken).
    exclude_values = {e["value"] for e in f["excludes"]}
    assert "Milk" in exclude_values and "chicken" in exclude_values and "unmapped" in exclude_values
    # Chicken appears as a protein facet (the Mexican recipe has it).
    assert any(p["value"] == "chicken" for p in f["proteins"])
    assert next(p["count"] for p in f["proteins"] if p["value"] == "chicken") == 2
    assert {s["value"] for s in f["sorts"]} >= {
        "popular",
        "protein_ratio",
        "price_low",
        "price_high",
    }
    assert set(f["ranges"].keys()) == {"kcal", "protein", "protein_ratio", "time"}
    # Diet facets are the derived column values.
    assert {d["value"] for d in f["diets"]} <= {
        "vegetarian", "pescatarian", "dairy_free", "gluten_free", "low_carb",
    }
    assert isinstance(cuisine_labels, set)


def test_personal_rating_round_trip(client):
    rid = client.get("/api/recipes", params={"cuisine": "Italian"}).json()["items"][0]["id"]

    detail = client.put(f"/api/recipes/{rid}/personal-rating", json={"rating": 4}).json()
    assert detail["personal_rating"] == 4

    listed = client.get("/api/recipes", params={"cuisine": "Italian"}).json()["items"][0]
    assert listed["personal_rating"] == 4

    updated = client.put(f"/api/recipes/{rid}/personal-rating", json={"rating": 2}).json()
    assert updated["personal_rating"] == 2

    cleared = client.put(f"/api/recipes/{rid}/personal-rating", json={"rating": None}).json()
    assert cleared["personal_rating"] is None


def test_personal_rating_filter_returns_only_rated(client):
    italian = client.get("/api/recipes", params={"cuisine": "Italian"}).json()["items"][0]
    mexican = client.get("/api/recipes", params={"cuisine": "Mexican"}).json()["items"][0]

    assert client.get("/api/recipes", params={"rated": "true"}).json()["total"] == 0

    client.put(f"/api/recipes/{mexican['id']}/personal-rating", json={"rating": 5})
    data = client.get("/api/recipes", params={"rated": "true"}).json()

    assert data["total"] == 1
    assert data["items"][0]["id"] == mexican["id"]
    assert data["items"][0]["personal_rating"] == 5
    assert data["items"][0]["id"] != italian["id"]


def test_personal_rating_rejects_invalid_and_hidden_recipes(client):
    rid = client.get("/api/recipes", params={"cuisine": "Italian"}).json()["items"][0]["id"]

    assert client.put(f"/api/recipes/{rid}/personal-rating", json={"rating": 0}).status_code == 422
    assert client.put(f"/api/recipes/{rid}/personal-rating", json={"rating": 6}).status_code == 422
    assert client.put("/api/recipes/4/personal-rating", json={"rating": 4}).status_code == 404


def test_wishlist_round_trip(client):
    rid = client.get("/api/recipes", params={"cuisine": "Italian"}).json()["items"][0]["id"]

    detail = client.put(f"/api/recipes/{rid}/wishlist", json={"wishlisted": True}).json()
    assert detail["wishlisted"] is True

    listed = client.get("/api/recipes", params={"cuisine": "Italian"}).json()["items"][0]
    assert listed["wishlisted"] is True

    cleared = client.put(f"/api/recipes/{rid}/wishlist", json={"wishlisted": False}).json()
    assert cleared["wishlisted"] is False

    listed = client.get("/api/recipes", params={"cuisine": "Italian"}).json()["items"][0]
    assert listed["wishlisted"] is False


def test_wishlist_filter_returns_only_wishlisted(client):
    italian = client.get("/api/recipes", params={"cuisine": "Italian"}).json()["items"][0]
    mexican = client.get("/api/recipes", params={"cuisine": "Mexican"}).json()["items"][0]

    assert client.get("/api/recipes", params={"wishlisted": "true"}).json()["total"] == 0

    client.put(f"/api/recipes/{mexican['id']}/wishlist", json={"wishlisted": True})
    data = client.get("/api/recipes", params={"wishlisted": "true"}).json()

    assert data["total"] == 1
    assert data["items"][0]["id"] == mexican["id"]
    assert data["items"][0]["wishlisted"] is True
    assert data["items"][0]["id"] != italian["id"]


def test_wishlist_rejects_hidden_recipes(client):
    assert client.put("/api/recipes/4/wishlist", json={"wishlisted": True}).status_code == 404
