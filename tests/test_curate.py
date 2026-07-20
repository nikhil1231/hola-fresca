"""Curation rule tests using synthetic recipes in a temp DB."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.models import Recipe
from app.db.session import init_db, make_engine, make_session_factory
from app.scraper.curate import CurationRules, curate


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(tmp_path / "curate.db")
    init_db(engine)
    return make_session_factory(engine)


def _recipe(**kw) -> Recipe:
    base = dict(
        source="hellofresh",
        source_id=kw.pop("source_id"),
        url="x",
        name=kw.pop("name", "Dish"),
        is_complete=1,
        energy_kcal=600.0,
        ratings_count=100,
        avg_rating=4.0,
        is_addon=0,
        source_created_at=datetime(2025, 1, 1),
    )
    base.update(kw)
    return Recipe(**base)


def _seed(factory, recipes: list[Recipe]) -> None:
    with factory() as s:
        s.add_all(recipes)
        s.commit()


def test_profile_a_keeps_proven_meal(factory):
    _seed(factory, [_recipe(source_id="a1")])
    report = curate(factory)
    assert report.curated == 1
    with factory() as s:
        assert s.query(Recipe).one().curated == 1


def test_cuts_incomplete_bundles_addons_lowkcal_unrated(factory):
    _seed(
        factory,
        [
            _recipe(source_id="ok", name="Good Dinner"),
            _recipe(source_id="stub", name="Stub", is_complete=0),
            _recipe(source_id="bundle", name="Gü Dessert Bundle"),
            _recipe(source_id="addon", name="Extra Chicken", is_addon=1),
            _recipe(source_id="sauce", name="Garlic Dip", energy_kcal=90.0),
            _recipe(source_id="unrated", name="New Thing", ratings_count=3),
        ],
    )
    report = curate(factory)
    assert report.curated == 1
    assert report.cut_incomplete == 1
    assert report.cut_bundle == 1
    assert report.cut_addon == 1
    assert report.cut_low_kcal == 1
    assert report.cut_unrated == 1
    with factory() as s:
        active = {r.source_id for r in s.query(Recipe).filter(Recipe.curated == 1)}
        assert active == {"ok"}


def test_dedup_keeps_newest_per_name(factory):
    _seed(
        factory,
        [
            _recipe(source_id="old", name="Chicken Curry", source_created_at=datetime(2023, 1, 1)),
            _recipe(source_id="new", name="Chicken Curry", source_created_at=datetime(2025, 6, 1)),
        ],
    )
    report = curate(factory)
    assert report.curated == 1
    assert report.cut_dup == 1
    with factory() as s:
        kept = s.query(Recipe).filter(Recipe.curated == 1).one()
        assert kept.source_id == "new"


def test_rules_are_reapplyable(factory):
    _seed(factory, [_recipe(source_id="a1", ratings_count=30), _recipe(source_id="a2", ratings_count=10, name="Other")])
    # Strict: only the well-rated one.
    assert curate(factory, rules=CurationRules(min_ratings=25)).curated == 1
    # Looser: both qualify, and the flag is recomputed (not additive).
    assert curate(factory, rules=CurationRules(min_ratings=5)).curated == 2
    # Back to strict again.
    assert curate(factory, rules=CurationRules(min_ratings=25)).curated == 1


def test_recency_exception_surfaces_new_recipes(factory):
    now = datetime.utcnow()
    _seed(
        factory,
        [
            _recipe(source_id="proven", name="Proven", ratings_count=100),
            _recipe(source_id="new_ok", name="New Popular", ratings_count=12,
                    source_created_at=now - timedelta(days=40)),
            _recipe(source_id="new_bare", name="Barely Rated", ratings_count=1,
                    source_created_at=now - timedelta(days=40)),
            _recipe(source_id="old_unrated", name="Old Unrated", ratings_count=12),
        ],
    )
    rep = curate(factory, rules=CurationRules(min_ratings=25, recent_days=120,
                                              recent_min_ratings=3, dedup_by_name=False))
    with factory() as s:
        active = {r.source_id for r in s.query(Recipe).filter(Recipe.curated == 1)}
    # Proven passes normally; New Popular via recency; barely-rated and old-unrated cut.
    assert active == {"proven", "new_ok"}
    assert rep.kept_recent == 1


def test_recency_can_be_disabled(factory):
    now = datetime.utcnow()
    _seed(factory, [_recipe(source_id="new", name="New", ratings_count=12,
                            source_created_at=now - timedelta(days=10))])
    rep = curate(factory, rules=CurationRules(recent_days=0, dedup_by_name=False))
    assert rep.curated == 0
    assert rep.kept_recent == 0
