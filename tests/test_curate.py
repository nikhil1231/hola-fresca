"""Curation rule tests using synthetic recipes in a temp DB."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.models import Recipe, RecipeIngredient
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
    base.setdefault(
        "ingredients",
        [RecipeIngredient(name="Rice", amount=100, unit="g", amount_g=100)],
    )
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


def test_dedup_collapses_renamed_versions_of_one_dish(factory):
    """The revisions a source renames between versions are the ones name-only
    dedup misses; the shared family code catches them."""
    _seed(
        factory,
        [
            _recipe(source_id="v1", name="Korean Honey Pork Noodles", family_code="R17179",
                    source_created_at=datetime(2023, 6, 1)),
            _recipe(source_id="v3", name="Korean Style BBQ Pork Noodles", family_code="R17179",
                    source_created_at=datetime(2024, 4, 1)),
            _recipe(source_id="other", name="Beef Ragu", family_code="R2001"),
        ],
    )
    report = curate(factory)
    assert report.cut_dup == 1
    with factory() as s:
        active = {r.source_id for r in s.query(Recipe).filter(Recipe.curated == 1)}
    assert active == {"v3", "other"}


def test_dedup_prefers_the_version_the_source_still_serves(factory):
    """A newer revision that was withdrawn loses to the one still on the menu."""
    _seed(
        factory,
        [
            _recipe(source_id="live", name="Old Name", family_code="R500",
                    source_active=1, source_created_at=datetime(2023, 1, 1)),
            _recipe(source_id="withdrawn", name="New Name", family_code="R500",
                    source_active=0, source_created_at=datetime(2025, 1, 1)),
        ],
    )
    curate(factory)
    with factory() as s:
        kept = s.query(Recipe).filter(Recipe.curated == 1).one()
    assert kept.source_id == "live"


def test_recipes_without_a_family_code_still_dedup_by_name(factory):
    _seed(
        factory,
        [
            _recipe(source_id="a", name="Same Dish", source_created_at=datetime(2023, 1, 1)),
            _recipe(source_id="b", name="Same Dish", source_created_at=datetime(2025, 1, 1)),
            _recipe(source_id="c", name="Other Dish"),
        ],
    )
    report = curate(factory)
    assert report.cut_dup == 1
    with factory() as s:
        active = {r.source_id for r in s.query(Recipe).filter(Recipe.curated == 1)}
    assert active == {"b", "c"}


def test_lineage_ratings_qualify_a_revision_with_none_of_its_own(factory):
    """A revision inherits the dish's rating count, which is what the source
    itself displays; without it the whole dish drops out of the library."""
    _seed(
        factory,
        [
            _recipe(source_id="revision", name="Proven Dish", ratings_count=0,
                    avg_rating=0.0, effective_ratings_count=1788, effective_rating=4.47),
        ],
    )
    report = curate(factory)
    assert report.curated == 1
    assert report.cut_unrated == 0


def test_effective_count_of_zero_is_honoured_not_treated_as_missing(factory):
    """An explicit zero must cut the recipe rather than fall back to the
    per-revision count, or the fallback would silently reinstate it."""
    _seed(
        factory,
        [
            _recipe(source_id="genuinely_unrated", name="Nobody Cooked This",
                    ratings_count=900, effective_ratings_count=0, effective_rating=0.0),
        ],
    )
    report = curate(factory)
    assert report.curated == 0
    assert report.cut_unrated == 1


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
                                              recent_min_ratings=3, dedup_versions=False))
    with factory() as s:
        active = {r.source_id for r in s.query(Recipe).filter(Recipe.curated == 1)}
    # Proven passes normally; New Popular via recency; barely-rated and old-unrated cut.
    assert active == {"proven", "new_ok"}
    assert rep.kept_recent == 1


def test_recency_can_be_disabled(factory):
    now = datetime.utcnow()
    _seed(factory, [_recipe(source_id="new", name="New", ratings_count=12,
                            source_created_at=now - timedelta(days=10))])
    rep = curate(factory, rules=CurationRules(recent_days=0, dedup_versions=False))
    assert rep.curated == 0
    assert rep.kept_recent == 0


def test_cuts_complete_recipes_with_all_zero_quantities(factory):
    _seed(
        factory,
        [
            _recipe(source_id="ok", name="Cookable"),
            _recipe(
                source_id="zero",
                name="Zero Amounts",
                ingredients=[
                    RecipeIngredient(name="Rice", amount=0, unit="g", amount_g=0),
                    RecipeIngredient(name="Pepper", amount=0, unit="g", amount_g=0),
                ],
            ),
            _recipe(
                source_id="mixed",
                name="Mixed Amounts",
                ingredients=[
                    RecipeIngredient(name="Rice", amount=0, unit="g", amount_g=0),
                    RecipeIngredient(name="Pepper", amount=1, unit="each", amount_g=None),
                ],
            ),
        ],
    )

    rep = curate(factory, rules=CurationRules(dedup_versions=False))

    assert rep.curated == 2
    assert rep.cut_zero_quantities == 1
    with factory() as s:
        active = {r.source_id for r in s.query(Recipe).filter(Recipe.curated == 1)}
    assert active == {"ok", "mixed"}


def test_a_recipe_whose_real_ingredients_are_all_zero_is_cut(factory):
    """The withdrawn draft: pork, potatoes and cauliflower all at zero, with
    only "3 tbsp Olive Oil" carrying a number. One quantified line out of
    eleven used to be enough to pass."""
    _seed(
        factory,
        [
            _recipe(source_id="cookable", name="Real Dinner"),
            _recipe(
                source_id="draft",
                name="Rosemary Pork Medallions",
                ingredients=[
                    RecipeIngredient(name="Pork Medallion", amount=0, amount_g=0),
                    RecipeIngredient(name="Baking Potato", amount=0, amount_g=0),
                    RecipeIngredient(name="Cauliflower", amount=0, amount_g=0),
                    RecipeIngredient(name="Olive Oil", amount=3, unit="tbsp", amount_g=45),
                ],
            ),
        ],
    )

    rep = curate(factory, rules=CurationRules(dedup_versions=False))

    assert rep.cut_zero_quantities == 1
    with factory() as s:
        active = {r.source_id for r in s.query(Recipe).filter(Recipe.curated == 1)}
    assert active == {"cookable"}


def test_a_partly_quantified_recipe_is_still_cookable(factory):
    """Real recipes do sometimes leave a line unpriced; the corpus tail stops
    at 0.6, so only a recipe that is almost entirely empty is cut."""
    _seed(
        factory,
        [
            _recipe(
                source_id="mostly",
                name="Mostly Quantified",
                ingredients=[
                    RecipeIngredient(name="Rice", amount=100, amount_g=100),
                    RecipeIngredient(name="Chicken", amount=250, amount_g=250),
                    RecipeIngredient(name="Garnish", amount=0, amount_g=0),
                ],
            ),
        ],
    )
    assert curate(factory, rules=CurationRules(dedup_versions=False)).curated == 1
