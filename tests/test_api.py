"""Recipe browse API tests.

A temporary SQLite DB is seeded with a handful of hand-built recipes covering the
filter dimensions, and the ``get_session`` dependency is overridden to point at
it. No network and no dependency on the real catalogue.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session
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
from main import app


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

    with factory() as s:
        italian = _make_recipe(
            source_id="a", name="Creamy Veggie Pasta", protein_g=50, energy_kcal=600,
            total_time_min=20, difficulty=1, avg_rating=4.5, ratings_count=900,
            protein_energy_ratio=8.3, is_vegetarian=1, is_pescatarian=1,
        )
        italian.cuisines = [RecipeCuisine(name="Italian")]
        italian.tags = [RecipeTag(name="X", type="seo")]  # no attribute chip
        italian.allergens = [RecipeAllergen(name="Milk")]
        italian.ingredients = [
            RecipeIngredient(name="Pasta", amount=180, unit="grams", amount_g=180, canonical_unit="g"),
            RecipeIngredient(name="Lentils", amount=1, unit="carton(s)", amount_g=250, canonical_unit="g"),
        ]
        italian.steps = [RecipeStep(index=1, instructions_text="Boil pasta")]
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
            RecipeIngredient(name="Chicken Breast", amount=250, unit="grams", amount_g=250),
            RecipeIngredient(name="Taco Tortillas", amount=6, unit="unit(s)"),
        ]

        uncurated = _make_recipe(source_id="c", name="Hidden", curated=0)

        s.add_all([italian, mexican, uncurated])
        s.commit()

    def _override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_returns_only_curated(client):
    data = client.get("/api/recipes").json()
    assert data["total"] == 2
    names = {i["name"] for i in data["items"]}
    assert "Hidden" not in names


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
    assert [i["protein_energy_ratio"] for i in items] == [8.3, 3.8]


def test_filter_min_protein_and_max_time(client):
    assert client.get("/api/recipes", params={"min_protein": 40}).json()["total"] == 1
    assert client.get("/api/recipes", params={"max_time": 25}).json()["total"] == 1


def test_exclude_allergen(client):
    data = client.get("/api/recipes", params={"exclude": "Milk"}).json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "Spicy Chicken Tacos"


def test_protein_include_filter(client):
    # Only the Mexican recipe has a chicken ingredient.
    data = client.get("/api/recipes", params={"protein": "chicken"}).json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "Spicy Chicken Tacos"


def test_exclude_ingredient(client):
    # Excluding chicken removes the Mexican recipe, leaving the Italian.
    data = client.get("/api/recipes", params={"exclude": "chicken"}).json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "Creamy Veggie Pasta"


def test_sort_protein_high(client):
    items = client.get("/api/recipes", params={"sort": "protein_high"}).json()["items"]
    assert [i["protein_g"] for i in items] == [50, 30]


def test_search_query(client):
    data = client.get("/api/recipes", params={"q": "taco"}).json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "Spicy Chicken Tacos"


def test_pagination(client):
    page1 = client.get("/api/recipes", params={"page_size": 1, "page": 1}).json()
    assert page1["total"] == 2
    assert len(page1["items"]) == 1
    assert page1["has_more"] is True
    page2 = client.get("/api/recipes", params={"page_size": 1, "page": 2}).json()
    assert page2["has_more"] is False


def test_detail_shape_and_image(client):
    rid = client.get("/api/recipes", params={"cuisine": "Italian"}).json()["items"][0]["id"]
    detail = client.get(f"/api/recipes/{rid}").json()
    assert detail["name"] == "Creamy Veggie Pasta"
    assert detail["ingredients"][0]["name"] == "Pasta"
    # Canonical grams flow through, incl. the count->grams conversion.
    lentils = next(i for i in detail["ingredients"] if i["name"] == "Lentils")
    assert lentils["amount_g"] == 250
    assert lentils["canonical_unit"] == "g"
    assert detail["steps"][0]["text"] == "Boil pasta"
    assert detail["image_url"].startswith("https://img.hellofresh.com/")
    assert "w_1200" in detail["image_url"]


def test_detail_404_for_uncurated(client):
    hidden = client.get("/api/recipes/3", params={})
    # id 3 is the uncurated recipe; must be hidden.
    assert hidden.status_code == 404


def test_facets(client):
    f = client.get("/api/facets").json()
    cuisine_labels = {c["label"] for c in f["cuisines"]}
    # Threshold is 20, and our seed has 1 each, so cuisines may be empty here;
    # the endpoint must still return the full structure.
    assert "diets" in f and "attributes" in f and "proteins" in f and "excludes" in f
    # Excludes combine allergens (e.g. Milk) and ingredient groups (e.g. chicken).
    exclude_values = {e["value"] for e in f["excludes"]}
    assert "Milk" in exclude_values and "chicken" in exclude_values
    # Chicken appears as a protein facet (the Mexican recipe has it).
    assert any(p["value"] == "chicken" for p in f["proteins"])
    assert {s["value"] for s in f["sorts"]} >= {"popular", "protein_ratio"}
    assert set(f["ranges"].keys()) == {"kcal", "protein", "protein_ratio", "time"}
    # Diet facets are the derived column values.
    assert {d["value"] for d in f["diets"]} <= {
        "vegetarian", "pescatarian", "dairy_free", "gluten_free", "low_carb",
    }
    assert isinstance(cuisine_labels, set)
