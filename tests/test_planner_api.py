"""Planner HTTP API tests for basket pricing and best-fit suggestions."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import planner as planner_api
from app.api.deps import get_planner_csv_path, get_session, get_session_factory
from app.db.models import Recipe, RecipeIngredient
from app.db.session import init_db, make_engine, make_session_factory
from app.mapping import service
from app.mapping.candidates import gather_candidates
from app.planner.basket import PackOption
from app.planner.index import Pack
from main import app
from tests.conftest import seed_candidates
from tests.test_planner_basket import write_freq_csv

KEY_RICE = "name:rice"
KEY_SALT = "name:salt"
KEY_MYSTERY = "name:mystery"
KEY_SAFFRON = "name:saffron"
KEY_BEANS = "name:beans"
KEY_MACARONI = "name:macaroni"

SID_RICE = "sid-rice"
SID_SALT = "sid-salt"
SID_MYSTERY = "sid-mystery"
SID_SAFFRON = "sid-saffron"
SID_BEANS = "sid-beans"
SID_MACARONI = "sid-macaroni"


def test_planner_index_loader_honours_recipe_subset(monkeypatch):
    requested = []
    sentinel = object()

    def get_index(*args, **kwargs):
        requested.append(kwargs["recipe_ids"])
        return sentinel

    monkeypatch.setattr(planner_api, "get_index", get_index)
    assert planner_api._load_planner_index(object(), [11, 22], None) is sentinel
    assert requested == [[11, 22]]


def test_pack_option_serializes_frozen_state():
    pack = Pack(
        sku="frozen-rice",
        product_name="Frozen Rice 500g",
        capacity_g=500,
        price=1.0,
        salvage=0.9,
        rank=1,
        match_type="exact",
        is_frozen=True,
    )
    option = PackOption(
        pack=pack,
        count=1,
        cost=1.0,
        capacity=500,
        leftover=200,
        unit_cost=0.002,
        cost_delta=0,
        leftover_delta=0,
    )

    assert planner_api._option_out(option).is_frozen is True


def _recipe(name: str, ingredients: list[RecipeIngredient], *, curated: int = 1) -> Recipe:
    return Recipe(
        source="hellofresh",
        source_id=name.lower().replace(" ", "-"),
        url="https://example.com/recipe",
        name=name,
        curated=curated,
        is_complete=1,
        base_yield=2,
        ingredients=ingredients,
    )


def _side(name: str, ingredients: list[RecipeIngredient]) -> Recipe:
    recipe = _recipe(name, ingredients)
    recipe.course = "side"
    return recipe


@pytest.fixture
def planner_client(tmp_path):
    engine = make_engine(tmp_path / "planner-api.db")
    init_db(engine)
    factory = make_session_factory(engine)
    csv_path = write_freq_csv(
        tmp_path / "ingredient_frequency.csv",
        [
            (KEY_RICE, SID_RICE, "Rice"),
            (KEY_SALT, SID_SALT, "Salt"),
            (KEY_MYSTERY, SID_MYSTERY, "Mystery"),
            (KEY_SAFFRON, SID_SAFFRON, "Saffron"),
            (KEY_BEANS, SID_BEANS, "Beans"),
            (KEY_MACARONI, SID_MACARONI, "Macaroni"),
        ],
    )

    with factory() as s:
        seed_candidates(
            s,
            KEY_RICE,
            "Rice",
            [{
                "sku": "rice",
                "name": "Rice 500g",
                "price": 1.0,
                "pack_value": 500,
                "pack_unit": "g",
                "is_frozen": True,
            }],
        )
        seed_candidates(
            s,
            KEY_SALT,
            "Salt",
            [{"sku": "salt", "name": "Salt 750g", "price": 0.65, "pack_value": 750, "pack_unit": "g"}],
        )
        seed_candidates(
            s,
            KEY_SAFFRON,
            "Saffron",
            [{"sku": "saffron", "name": "Saffron each", "price": 3.0, "pack_value": 1, "pack_unit": "each"}],
        )
        seed_candidates(
            s,
            KEY_BEANS,
            "Beans",
            [{"sku": "beans", "name": "Beans 400g", "price": 2.0, "pack_value": 400, "pack_unit": "g"}],
        )
        seed_candidates(
            s,
            KEY_MACARONI,
            "Macaroni",
            [{"sku": "macaroni", "name": "Macaroni 500g", "price": 1.0, "pack_value": 500, "pack_unit": "g"}],
        )

        service.save_decision(
            s,
            gather_candidates(s, KEY_RICE),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="rice", rank=1)],
            ),
        )
        service.save_decision(
            s,
            gather_candidates(s, KEY_SALT),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="salt", rank=1)],
                pantry_staple=True,
            ),
        )
        service.save_decision(
            s,
            gather_candidates(s, KEY_SAFFRON),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="saffron", rank=1)],
            ),
        )
        service.save_decision(
            s,
            gather_candidates(s, KEY_BEANS),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="beans", rank=1)],
            ),
        )
        service.save_decision(
            s,
            gather_candidates(s, KEY_MACARONI),
            service.DecisionInput(
                status="approved",
                accepted=[service.AcceptedInput(sku="macaroni", rank=1)],
            ),
        )

        pinned = _recipe(
            "Rice Bowl",
            [
                RecipeIngredient(name="Rice", source_ingredient_id=SID_RICE, amount=300, unit="g", amount_g=300),
                RecipeIngredient(name="Salt", source_ingredient_id=SID_SALT, amount=2, unit="g", amount_g=2),
                RecipeIngredient(name="Mystery", source_ingredient_id=SID_MYSTERY, amount=50, unit="g", amount_g=50),
                RecipeIngredient(name="Saffron", source_ingredient_id=SID_SAFFRON, amount=1, unit="each", amount_g=1),
                RecipeIngredient(name="Untracked", source_ingredient_id="sid-unknown", amount=1, unit="piece", amount_g=25),
            ],
        )
        shared = _recipe(
            "Rice Patties",
            [RecipeIngredient(name="Rice", source_ingredient_id=SID_RICE, amount=100, unit="g", amount_g=100)],
        )
        standalone = _recipe(
            "Bean Stew",
            [RecipeIngredient(name="Beans", source_ingredient_id=SID_BEANS, amount=100, unit="g", amount_g=100)],
        )
        gap_heavy = _recipe(
            "Mystery Plate",
            [
                RecipeIngredient(name="Mystery", source_ingredient_id=SID_MYSTERY, amount=50, unit="g", amount_g=50),
                RecipeIngredient(name="Saffron", source_ingredient_id=SID_SAFFRON, amount=1, unit="each", amount_g=1),
                RecipeIngredient(name="Unknown", source_ingredient_id="sid-unknown-2", amount=1, unit="each", amount_g=25),
            ],
        )
        unpriceable_only = _recipe(
            "Saffron Rice",
            [RecipeIngredient(name="Saffron", source_ingredient_id=SID_SAFFRON, amount=1, unit="each", amount_g=1)],
        )
        speedy = _recipe(
            "Speedy Cajun Style Chicken Macaroni",
            [
                RecipeIngredient(name="Rice", source_ingredient_id=SID_RICE, amount=100, unit="g", amount_g=100),
                RecipeIngredient(name="Macaroni", source_ingredient_id=SID_MACARONI, amount=100, unit="g", amount_g=100),
            ],
        )
        side = _side(
            "Rice Side",
            [RecipeIngredient(name="Rice", source_ingredient_id=SID_RICE, amount=100, unit="g", amount_g=100)],
        )
        hidden = _recipe(
            "Hidden Rice",
            [RecipeIngredient(name="Rice", source_ingredient_id=SID_RICE, amount=100, unit="g", amount_g=100)],
            curated=0,
        )
        s.add_all([pinned, shared, standalone, gap_heavy, unpriceable_only, speedy, side, hidden])
        s.commit()
        ids = {
            "pinned": pinned.id,
            "shared": shared.id,
            "standalone": standalone.id,
            "gap_heavy": gap_heavy.id,
            "hidden": hidden.id,
            "speedy": speedy.id,
            "side": side.id,
        }

    def _override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_planner_csv_path] = lambda: csv_path
    yield TestClient(app), ids
    app.dependency_overrides.clear()


def test_basket_serializes_totals_and_all_buckets(planner_client):
    client, ids = planner_client
    data = client.post(
        "/api/planner/basket",
        json={"selections": [{"recipe_id": ids["pinned"], "portions": 2}]},
    ).json()

    assert data["cost"] == 1.0
    assert data["score"] >= data["cost"]
    assert data["staples"] == ["Salt"]
    assert data["unmapped"] == ["Mystery"]
    assert data["unpriceable"] == ["Saffron"]
    assert data["untracked_lines"] == 1

    rice = data["lines"][0]
    assert rice["name"] == "Rice"
    assert rice["need_g"] == 300
    assert rice["capacity_g"] == 500
    assert rice["leftover_g"] == 200
    assert rice["cost"] == 1.0
    assert rice["packs"] == 1
    assert rice["choices"][0]["sku"] == "rice"
    assert rice["choices"][0]["is_frozen"] is True
    assert rice["contributions"][0]["recipe_id"] == ids["pinned"]
    assert rice["contributions"][0]["recipe_name"] == "Rice Bowl"
    assert rice["contributions"][0]["grams"] == 300


def test_a_pack_preference_is_recorded_against_the_mapping(planner_client):
    client, _ = planner_client
    response = client.put(
        "/api/planner/preferences/pack", json={"ingredient_key": KEY_RICE, "sku": "rice"}
    )

    assert response.status_code == 200
    assert response.json() == {"ingredient_key": KEY_RICE, "sku": "rice"}
    # And clearing it hands the size decision back to the planner.
    cleared = client.put(
        "/api/planner/preferences/pack", json={"ingredient_key": KEY_RICE, "sku": None}
    )
    assert cleared.json()["sku"] is None


def test_a_pack_preference_has_to_name_an_approved_product(planner_client):
    """Otherwise the basket pins itself to something the mapping never allowed."""
    client, _ = planner_client

    rejected = client.put(
        "/api/planner/preferences/pack", json={"ingredient_key": KEY_RICE, "sku": "beans"}
    )
    missing = client.put(
        "/api/planner/preferences/pack", json={"ingredient_key": "name:nope", "sku": None}
    )

    assert rejected.status_code == 400
    assert missing.status_code == 404


def test_basket_portions_scale_from_base_yield(planner_client):
    client, ids = planner_client
    data = client.post(
        "/api/planner/basket",
        json={"selections": [{"recipe_id": ids["pinned"], "portions": 4}]},
    ).json()
    rice = data["lines"][0]
    assert rice["need_g"] == 600
    assert rice["packs"] == 2
    assert data["cost"] == 2.0


def test_basket_snap_uses_the_saved_household_tolerance(planner_client):
    client, ids = planner_client
    request = {"selections": [{"recipe_id": ids["pinned"], "portions": 4}]}

    default_line = client.post("/api/planner/basket", json=request).json()["lines"][0]
    assert default_line["need_g"] == 600
    assert default_line["snap"] is None, "500g is more than 10% short of 600g"

    saved = client.put(
        "/api/schedule/settings", json={"pack_shortfall_tolerance_pct": 20}
    )
    assert saved.status_code == 200
    tolerant_line = client.post("/api/planner/basket", json=request).json()["lines"][0]

    assert tolerant_line["snap"]["snapped_need_g"] == 500
    assert tolerant_line["snap"]["reduction_pct"] == pytest.approx(16.7, abs=0.1)


def test_basket_allows_empty_selection(planner_client):
    client, _ = planner_client
    data = client.post("/api/planner/basket", json={"selections": []}).json()
    assert data == {
        "lines": [],
        "staples": [],
        "unmapped": [],
        "unpriceable": [],
        "sold_out": [],
        "untracked_lines": 0,
        "cost": 0.0,
        "waste_gbp": 0.0,
        "score": 0.0,
        "stock_checked_at": None,
    }


def test_basket_rejects_unknown_or_uncurated_recipes(planner_client):
    client, ids = planner_client
    missing = client.post(
        "/api/planner/basket",
        json={"selections": [{"recipe_id": 9999, "portions": 2}]},
    )
    hidden = client.post(
        "/api/planner/basket",
        json={"selections": [{"recipe_id": ids["hidden"], "portions": 2}]},
    )
    assert missing.status_code == 400
    assert hidden.status_code == 400


def test_browse_excludes_recipes_with_pricing_gaps(planner_client):
    client, _ = planner_client
    data = client.get("/api/recipes", params={"exclude": "unmapped"}).json()

    assert data["total"] == 3
    assert {item["name"] for item in data["items"]} == {
        "Rice Patties",
        "Bean Stew",
        "Speedy Cajun Style Chicken Macaroni",
    }
    assert all(item["intrinsic_gap_count"] == 0 for item in data["items"])


def test_suggestions_rank_shared_marginal_cost_first(planner_client):
    client, ids = planner_client
    data = client.post(
        "/api/planner/suggestions",
        json={
            "selections": [{"recipe_id": ids["pinned"], "portions": 2}],
            "filters": {"exclude": ["unmapped"]},
        },
    ).json()

    assert data["total"] == 3
    assert [item["name"] for item in data["items"]] == [
        "Rice Patties",
        "Speedy Cajun Style Chicken Macaroni",
        "Bean Stew",
    ]
    assert data["items"][0]["marginal_score"] < 0.0
    assert data["items"][0]["standalone_score"] > data["items"][0]["marginal_score"]
    assert data["items"][0]["ranking_score"] < data["items"][1]["ranking_score"]
    assert data["items"][0]["shared_ingredient_count"] == 1

    assert data["items"][0]["marginal_cost"] < data["items"][0]["standalone_cost"]

    bean = data["items"][2]
    assert bean["shared_ingredient_count"] == 0
    assert bean["marginal_score"] == bean["standalone_score"]
    assert bean["marginal_cost"] == bean["standalone_cost"]
    assert bean["standalone_score"] > 0


def test_suggestions_apply_filters_and_pagination(planner_client):
    client, ids = planner_client
    data = client.post(
        "/api/planner/suggestions",
        json={
            "selections": [{"recipe_id": ids["pinned"], "portions": 2}],
            "filters": {"q": "bean"},
            "page": 1,
            "page_size": 1,
        },
    ).json()

    assert data["total"] == 1
    assert data["has_more"] is False
    assert data["items"][0]["name"] == "Bean Stew"


def test_suggestions_count_what_lies_outside_the_library(planner_client):
    """Best fit ranks the library, so it has to say when that is the whole answer.

    Suggestions are the endpoint browse reads while a week is being planned —
    the screen people search on most — and it can never return an uncurated
    recipe, because a recipe the planner cannot price into a week has no
    marginal cost to rank by. Counting them is what lets the client offer a
    plain search instead of leaving "no matches" as the last word.
    """
    client, ids = planner_client
    data = client.post(
        "/api/planner/suggestions",
        json={
            "selections": [{"recipe_id": ids["pinned"], "portions": 2}],
            "filters": {"q": "hidden"},
        },
    ).json()

    assert data["total"] == 0
    assert data["uncurated_total"] == 1
    # Not paid for when the ranking already had something to show.
    ranked = client.post(
        "/api/planner/suggestions",
        json={
            "selections": [{"recipe_id": ids["pinned"], "portions": 2}],
            "filters": {"q": "bean"},
        },
    ).json()
    assert ranked["uncurated_total"] is None


def test_suggestions_apply_fuzzy_search(planner_client):
    client, ids = planner_client
    data = client.post(
        "/api/planner/suggestions",
        json={
            "selections": [{"recipe_id": ids["pinned"], "portions": 2}],
            "filters": {"q": "speedy mac"},
        },
    ).json()

    assert data["total"] == 1
    assert data["items"][0]["name"] == "Speedy Cajun Style Chicken Macaroni"


def test_suggestions_apply_course_filter(planner_client):
    client, ids = planner_client
    data = client.post(
        "/api/planner/suggestions",
        json={
            "selections": [{"recipe_id": ids["pinned"], "portions": 2}],
            "filters": {"course": ["side"]},
        },
    ).json()

    assert data["total"] == 1
    assert data["items"][0]["name"] == "Rice Side"
    assert data["items"][0]["course"] == "side"


def test_suggestions_can_exclude_unmapped_recipes(planner_client):
    client, ids = planner_client
    data = client.post(
        "/api/planner/suggestions",
        json={
            "selections": [{"recipe_id": ids["pinned"], "portions": 2}],
            "filters": {"exclude": ["unmapped"]},
        },
    ).json()

    assert data["total"] == 3
    assert [item["name"] for item in data["items"]] == [
        "Rice Patties",
        "Speedy Cajun Style Chicken Macaroni",
        "Bean Stew",
    ]
