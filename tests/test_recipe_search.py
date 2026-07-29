"""Search matching and relevance ranking.

The tuning lives in these numbers, so the cases that fixed them are pinned here:
a query is an AND over its tokens, typos are forgiven only where forgiving them
is safe, and the ordering is by match quality rather than by fame.
"""
from __future__ import annotations

import pytest

from app.api.recipes import _ranked_recipe_ids, _relevance
from app.db.models import Recipe
from app.db.session import init_db, make_engine, make_session_factory


def _matches(query: str, name: str, headline: str | None = None) -> bool:
    return _relevance(query, name, headline) is not None


# --- what counts as a match -------------------------------------------------

def test_every_query_token_must_be_answered():
    """The regression: one shared word used to carry a whole phrase, so
    "korean bbq noodles" returned 344 recipes on a 5,198-recipe library."""
    assert _matches("korean bbq noodles", "Korean Style BBQ Pork Noodles")
    assert not _matches("korean bbq noodles", "Thai Veggie Noodles")
    assert not _matches("korean bbq noodles", "BBQ Beef Cheeseburger")
    assert not _matches("korean bbq noodles", "Beef Stir-Fry")


def test_words_may_be_separated_in_the_title():
    # The case fuzzy search was added for: not a contiguous substring.
    assert _matches("korean bbq", "Korean Style BBQ Pork Noodles")


def test_headline_counts_as_searchable_text():
    assert _matches("lime peanuts", "Korean Style BBQ Pork Noodles",
                    "with Pepper, Lime and Peanuts")


def test_a_prefix_matches_but_an_unrelated_word_does_not():
    assert _matches("mac", "Speedy Cajun Style Chicken Macaroni")
    assert not _matches("mac", "Spicy Chicken Tacos")


@pytest.mark.parametrize("typo, name", [
    ("koren bbq", "Korean Style BBQ Pork Noodles"),
    ("chikcen curry", "Chicken Korma Style Curry"),
    ("noodels", "Thai Veggie Noodles"),
])
def test_typos_are_forgiven(typo, name):
    assert _matches(typo, name)


def test_short_tokens_are_not_typo_corrected():
    """Three letters are within an edit of half the corpus, so "bbq" has to be
    spelt right; correcting it would undo the whole point of the AND."""
    assert not _matches("bbq", "BBC Bacon Bap")
    assert not _matches("bap", "BBQ Beef Cheeseburger")


def test_an_unrelated_query_matches_nothing():
    assert not _matches("banana pudding", "Korean Style BBQ Pork Noodles")


# --- ranking ----------------------------------------------------------------

def test_a_title_match_outranks_a_headline_match():
    title = _relevance("peanuts", "Peanut Noodles", "with Lime")
    headline = _relevance("peanuts", "Pork Noodles", "with Lime and Peanuts")
    assert title is not None and headline is not None
    assert title > headline


def test_an_exact_word_outranks_a_word_that_merely_contains_it():
    assert _relevance("curry", "Chicken Curry", None) > _relevance(
        "curry", "Currywurst Bowl", None
    )


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(tmp_path / "search.db")
    init_db(engine)
    f = make_session_factory(engine)
    with f() as s:
        s.add_all([
            # Far more popular, but only a headline match.
            Recipe(source="hellofresh", source_id="famous", url="x", curated=1,
                   is_complete=1, name="Sticky Pork Bowl",
                   headline="with Noodles and Greens", effective_ratings_count=50000),
            # Barely rated, but the words are right there in the name.
            Recipe(source="hellofresh", source_id="exact", url="x", curated=1,
                   is_complete=1, name="Pork Noodles",
                   headline="with Lime", effective_ratings_count=5),
            Recipe(source="hellofresh", source_id="other", url="x", curated=1,
                   is_complete=1, name="Beef Tacos", effective_ratings_count=900),
        ])
        s.commit()
    return f


_NO_FILTERS = dict(
    cuisine=[], diet=[], tag=[], protein=[], max_time=None, min_protein=None,
    min_protein_ratio=None, max_kcal=None, difficulty=None, exclude=[],
)


def test_relevance_beats_popularity(factory):
    with factory() as s:
        ids = _ranked_recipe_ids(s, dict(q="pork noodles", **_NO_FILTERS))
        names = [s.get(Recipe, i).name for i in ids]
    assert names == ["Pork Noodles", "Sticky Pork Bowl"]


def test_popularity_still_breaks_ties(factory):
    """Two equally good matches are ordered by how well known they are."""
    with factory() as s:
        s.add(Recipe(source="hellofresh", source_id="dup", url="x", curated=1,
                     is_complete=1, name="Pork Noodles", effective_ratings_count=9000))
        s.commit()
        ids = _ranked_recipe_ids(s, dict(q="pork noodles", **_NO_FILTERS))
        counts = [s.get(Recipe, i).effective_ratings_count for i in ids[:2]]
    assert counts == [9000, 5]


def test_no_query_returns_everything_unranked(factory):
    with factory() as s:
        assert len(_ranked_recipe_ids(s, dict(q=None, **_NO_FILTERS))) == 3


# --- course filtering -------------------------------------------------------

@pytest.fixture
def courses(tmp_path):
    engine = make_engine(tmp_path / "course.db")
    init_db(engine)
    f = make_session_factory(engine)
    with f() as s:
        s.add_all([
            Recipe(source="hellofresh", source_id="m", url="x", curated=1, is_complete=1,
                   name="Chicken Curry", course="main", effective_ratings_count=100),
            Recipe(source="hellofresh", source_id="s", url="x", curated=1, is_complete=1,
                   name="Garlic Bread Side", course="side", effective_ratings_count=100),
            Recipe(source="hellofresh", source_id="d", url="x", curated=1, is_complete=1,
                   name="Chocolate Brownie", course="dessert", effective_ratings_count=100),
            Recipe(source="hellofresh", source_id="p", url="x", curated=1, is_complete=1,
                   name="Houmous", course="product", effective_ratings_count=100),
            # A row written before the column existed.
            Recipe(source="hellofresh", source_id="legacy", url="x", curated=1,
                   is_complete=1, name="Legacy Dinner", course=None,
                   effective_ratings_count=100),
        ])
        s.commit()
    return f


def _names(factory, **overrides):
    filters = dict(q=None, **_NO_FILTERS)
    filters.update(overrides)
    with factory() as s:
        return {s.get(Recipe, i).name for i in _ranked_recipe_ids(s, filters)}


def test_browse_shows_mains_only_by_default(courses):
    assert _names(courses) == {"Chicken Curry", "Legacy Dinner"}


def test_a_row_predating_the_column_reads_as_a_main(courses):
    """Otherwise every recipe vanishes from browse between the deploy and the
    next enrich pass."""
    assert "Legacy Dinner" in _names(courses)
    assert "Legacy Dinner" in _names(courses, course=["main"])


def test_sides_are_opt_in(courses):
    assert _names(courses, course=["side"]) == {"Garlic Bread Side"}
    assert _names(courses, course=["main", "side"]) == {
        "Chicken Curry", "Legacy Dinner", "Garlic Bread Side"
    }


def test_asking_for_every_course_returns_everything(courses):
    assert len(_names(courses, course=["main", "side", "dessert", "product"])) == 5


def test_search_is_scoped_to_mains_too(courses):
    """A search for "chocolate" must not surface a dessert while browse hides
    desserts; the two would disagree about what the library contains."""
    assert _names(courses, q="chocolate") == set()
    assert _names(courses, q="chocolate", course=["dessert"]) == {"Chocolate Brownie"}
