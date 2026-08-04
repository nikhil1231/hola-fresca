"""Backfill tests: refresh source fields without disturbing anything else.

The point of this pass is that it is safe to run against a live library, so the
tests care as much about what it leaves alone as about what it updates.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config as app_config
from tests.conftest import user_id
from app.db.models import PersonalRecipeRating, Recipe, RecipeEdit, RecipeStep
from app.db.session import init_db, make_engine, make_session_factory
from app.scraper import storage
from app.scraper.backfill import backfill_source_fields
from app.scraper.sources.hellofresh import HelloFreshSource

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(app_config, "DB_PATH", tmp_path / "test.db")
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def source() -> HelloFreshSource:
    return HelloFreshSource()


def _seed(env, **overrides):
    """A recipe row as an older parser would have written it, plus its payload."""
    payload = json.loads((FIXTURES / "hellofresh_complete.json").read_text())
    source_id = payload["recipeId"]
    storage.write_raw("hellofresh", source_id, payload, base_dir=app_config.RAW_DIR)

    fields = dict(
        source="hellofresh",
        source_id=source_id,
        url="x",
        name="Crispy Chicken Goujons and Cheesy Wedges",
        is_complete=1,
        energy_kcal=713.0,
        avg_rating=3.31,
        ratings_count=422,
    )
    fields.update(overrides)
    with env() as session:
        recipe = Recipe(**fields)
        recipe.steps = [RecipeStep(index=1, instructions_text="Older instruction")]
        session.add(recipe)
        session.commit()
    return source_id


def test_backfill_fills_lineage_ratings_and_revision_identity(env, source):
    source_id = _seed(env)

    report = backfill_source_fields(source, env)
    assert report.examined == 1
    assert report.updated == 1

    with env() as session:
        r = session.query(Recipe).one()
        assert r.source_id == source_id
        assert r.aggregate_ratings_count == 2292
        assert r.effective_ratings_count == 2292
        assert r.effective_rating and 4.3 < r.effective_rating < 4.4
        # The per-revision figures stay on the row untouched.
        assert r.ratings_count == 422
        assert r.family_code == "R17041"
        assert r.unique_recipe_code == "R17041-18"
        assert r.source_active == 1
        assert r.steps[0].image_path == "/693adbb51101204cae74ecbc/step-baa42500.jpg"
        assert r.steps[0].instructions_text.startswith("Preheat your oven")


def test_backfill_preserves_human_and_derived_state(env, source):
    """Re-normalising would replace the row and take these with it. This pass
    updates in place precisely so a live library can be refreshed safely."""
    source_id = _seed(env, curated=1, flagged_suspicious=1, protein_g=51.5)
    with env() as session:
        recipe = session.query(Recipe).one()
        session.add(
            PersonalRecipeRating(user_id=user_id(session), recipe_id=recipe.id, rating=5)
        )
        session.add(
            RecipeEdit(
                recipe_id=recipe.id,
                field="energy_kcal",
                old_value=713.0,
                new_value=690.0,
                source="human",
            )
        )
        session.commit()
        recipe_id = recipe.id

    backfill_source_fields(source, env)

    with env() as session:
        r = session.query(Recipe).one()
        assert r.id == recipe_id, "the row must keep its identity, not be replaced"
        assert r.curated == 1
        assert r.flagged_suspicious == 1
        assert r.protein_g == 51.5
        assert session.query(PersonalRecipeRating).count() == 1
        assert session.query(RecipeEdit).one().new_value == 690.0


def test_backfill_corrects_a_drifted_rating_count(env, source):
    _seed(env, ratings_count=17)
    report = backfill_source_fields(source, env)
    assert report.ratings_corrected == 1
    with env() as session:
        assert session.query(Recipe).one().ratings_count == 422


def test_backfill_is_idempotent(env, source):
    _seed(env)
    backfill_source_fields(source, env)
    second = backfill_source_fields(source, env)
    assert second.examined == 1
    assert second.updated == 0


def test_backfill_reports_a_missing_payload_without_touching_the_row(env, source):
    _seed(env)
    storage.raw_path("hellofresh", "693adbb51101204cae74ecbc", app_config.RAW_DIR).unlink()

    report = backfill_source_fields(source, env)
    assert report.missing_raw == 1
    assert report.updated == 0
    with env() as session:
        assert session.query(Recipe).one().ratings_count == 422
