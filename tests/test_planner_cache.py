"""Catalogue revisions and the planner's incremental process-wide snapshot."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.db.models import (
    PersonalRecipeRating,
    PersonalRecipeWishlist,
    PlanSelection,
    PlanSettings,
    PlanWeek,
    Recipe,
    RecipeCookMap,
    RecipeIngredient,
    User,
    UserRecipeHide,
)
from app.db.planner_revision import INGREDIENT_TABLES, RECIPE_TABLES, trigger_name
from app.db.session import init_db, make_engine, make_session_factory
from app.planner import cache
from tests.test_planner_basket import write_freq_csv


@pytest.fixture
def cache_db(tmp_path):
    engine = make_engine(tmp_path / "cache.db")
    init_db(engine)
    factory = make_session_factory(engine)
    csv_path = write_freq_csv(
        tmp_path / "ingredient_frequency.csv",
        [("name:rice", "sid-rice", "Rice")],
    )
    with factory() as session:
        recipes = []
        for index in (1, 2):
            recipe = Recipe(
                source="hellofresh",
                source_id=f"recipe-{index}",
                url=f"https://example.com/{index}",
                name=f"Recipe {index}",
                curated=1,
                is_complete=1,
                base_yield=2,
                ingredients=[
                    RecipeIngredient(
                        source_ingredient_id="sid-rice",
                        name="Rice",
                        amount=100,
                        unit="grams",
                        amount_g=100,
                    )
                ],
            )
            session.add(recipe)
            recipes.append(recipe)
        session.commit()
        ids = [recipe.id for recipe in recipes]
    with cache._LOCK:
        cache._CACHE.clear()
    yield engine, factory, csv_path, ids
    with cache._LOCK:
        cache._CACHE.clear()


def _revisions(engine) -> tuple[int, int]:
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                text(
                    "SELECT recipe_revision, ingredient_revision "
                    "FROM planner_cache_state WHERE id = 1"
                )
            ).one()
        )


def test_fresh_database_has_revision_row_and_every_trigger(cache_db):
    engine, _factory, _csv_path, _ids = cache_db
    with engine.connect() as connection:
        triggers = set(
            connection.scalars(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        )
    expected = {
        trigger_name(table, operation)
        for table in (*RECIPE_TABLES, *INGREDIENT_TABLES)
        for operation in ("INSERT", "UPDATE", "DELETE")
    }
    assert expected <= triggers
    assert all(value >= 1 for value in _revisions(engine))


def test_external_sql_changes_bump_only_the_relevant_generation(cache_db):
    engine, _factory, _csv_path, ids = cache_db
    recipe_before, ingredient_before = _revisions(engine)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE recipes SET name = 'Externally changed' WHERE id = :id"),
            {"id": ids[0]},
        )
        connection.execute(
            text("UPDATE recipe_ingredients SET amount_g = 125 WHERE recipe_id = :id"),
            {"id": ids[0]},
        )
    assert _revisions(engine) == (recipe_before + 2, ingredient_before)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO products (retailer, sku, name, price, created_at) "
                "VALUES ('ocado', 'raw-rice', 'Raw Rice', 1.5, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ingredient_mappings "
                "(retailer, ingredient_key, name, status, pantry_staple, line_count, "
                " unit_kind, needs_substitution, "
                " created_at, updated_at) "
                "VALUES ('ocado', 'name:rice', 'Rice', 'approved', 0, 1, "
                " 'mass', 0, "
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        mapping_id = connection.execute(
            text("SELECT id FROM ingredient_mappings WHERE ingredient_key = 'name:rice'")
        ).scalar_one()
        product_id = connection.execute(
            text("SELECT id FROM products WHERE sku = 'raw-rice'")
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO ingredient_mapping_products "
                "(mapping_id, product_id, sku, rank, match_type, accepted, source) "
                "VALUES (:mapping_id, :product_id, 'raw-rice', 1, 'exact', 1, 'human')"
            ),
            {"mapping_id": mapping_id, "product_id": product_id},
        )
    assert _revisions(engine) == (recipe_before + 2, ingredient_before + 3)


def test_personal_plan_schedule_hide_and_cook_map_writes_do_not_bump(cache_db):
    engine, factory, _csv_path, ids = cache_db
    before = _revisions(engine)
    with factory() as session:
        user_id = session.query(User.id).order_by(User.id).limit(1).scalar()
        session.add_all(
            [
                PersonalRecipeRating(user_id=user_id, recipe_id=ids[0], rating=5),
                PersonalRecipeWishlist(user_id=user_id, recipe_id=ids[0]),
                UserRecipeHide(user_id=user_id, recipe_id=ids[1]),
                PlanSettings(user_id=user_id, anchor_week_start="2026-08-03"),
                PlanWeek(user_id=user_id, week_start="2026-08-03", skipped=0),
                PlanSelection(
                    user_id=user_id,
                    week_start="2026-08-03",
                    recipe_id=ids[0],
                    portions=4,
                ),
                RecipeCookMap(
                    recipe_id=ids[0],
                    status="ready",
                    graph_json=json.dumps({"nodes": [], "edges": []}),
                    source_fingerprint="test",
                ),
            ]
        )
        session.commit()
    assert _revisions(engine) == before


def test_targeted_hydration_reuses_catalogue_and_known_or_missing_ids(cache_db, monkeypatch):
    _engine, factory, csv_path, ids = cache_db
    catalogue_calls = 0
    hydrated: list[set[int] | None] = []
    real_catalogue = cache.load_catalogue
    real_hydrate = cache.hydrate_recipes

    def load_catalogue(*args, **kwargs):
        nonlocal catalogue_calls
        catalogue_calls += 1
        return real_catalogue(*args, **kwargs)

    def hydrate(*args, recipe_ids, **kwargs):
        hydrated.append(None if recipe_ids is None else set(recipe_ids))
        return real_hydrate(*args, recipe_ids=recipe_ids, **kwargs)

    monkeypatch.setattr(cache, "load_catalogue", load_catalogue)
    monkeypatch.setattr(cache, "hydrate_recipes", hydrate)

    first = cache.get_index(factory, recipe_ids=[ids[0], 999_999], csv_path=csv_path)
    second = cache.get_index(factory, recipe_ids=[ids[0], 999_999], csv_path=csv_path)
    cache.get_index(factory, recipe_ids=[ids[1]], csv_path=csv_path)
    cache.get_ranking(factory, [], candidate_portions=4, csv_path=csv_path)

    assert first is second
    assert catalogue_calls == 1
    assert hydrated == [{ids[0], 999_999}, {ids[1]}, None]
    assert set(first.recipes) == set(ids)


def test_recipe_revision_reuses_ingredients_and_rejects_racing_build(cache_db, monkeypatch):
    engine, factory, csv_path, ids = cache_db
    real_hydrate = cache.hydrate_recipes
    raced = False
    calls = 0

    def hydrate(*args, recipe_ids, **kwargs):
        nonlocal raced, calls
        calls += 1
        loaded = real_hydrate(*args, recipe_ids=recipe_ids, **kwargs)
        if not raced:
            raced = True
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE recipes SET name = 'Won the race' WHERE id = :id"),
                    {"id": ids[0]},
                )
        return loaded

    monkeypatch.setattr(cache, "hydrate_recipes", hydrate)
    index = cache.get_index(factory, recipe_ids=[ids[0]], csv_path=csv_path)

    assert calls == 2
    assert index.recipes[ids[0]].name == "Won the race"
    assert cache.get_index(factory, recipe_ids=[ids[0]], csv_path=csv_path) is index


def test_standalone_prices_are_cached_per_requested_recipe(cache_db, monkeypatch):
    _engine, factory, csv_path, ids = cache_db
    scored: list[int] = []
    real_score = cache.score_basket

    def score(index, selections, **kwargs):
        scored.append(selections[0].recipe_id)
        return real_score(index, selections, **kwargs)

    monkeypatch.setattr(cache, "score_basket", score)
    cache.get_standalone_prices(
        factory, servings=4, recipe_ids=[ids[0]], csv_path=csv_path
    )
    prices = cache.get_standalone_prices(
        factory, servings=4, recipe_ids=ids, csv_path=csv_path
    )

    assert scored == ids
    assert set(prices) == set(ids)
