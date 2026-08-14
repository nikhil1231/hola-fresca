"""The planner cache distinguishes catalogue edits from personal writes."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.session import make_engine, make_session_factory
from app.planner import cache
from app.planner.basket import BasketScore, Selection, score_basket
from app.planner.index import Ingredient, Need, Pack, PlanIndex, PlanRecipe
from app.planner.ranking import rank_candidates


def _factory(tmp_path):
    engine = make_engine(tmp_path / "planner-cache.db")
    with engine.begin() as connection:
        connection.execute(text("create table personal_note (value text)"))
    return engine, make_session_factory(engine)


def test_personal_write_does_not_discard_catalogue_index(tmp_path, monkeypatch):
    engine, factory = _factory(tmp_path)
    loads = 0

    def load(*args, **kwargs):
        nonlocal loads
        loads += 1
        return PlanIndex()

    monkeypatch.setattr(cache, "load_index", load)
    cache._CACHE.clear()
    try:
        first = cache.get_index(factory)
        with factory() as session:
            with cache.preserve_after_personal_write(session):
                session.execute(
                    text("insert into personal_note (value) values ('planned recipe')")
                )
                session.commit()

        assert cache.get_index(factory) is first
        assert loads == 1
    finally:
        cache._CACHE.clear()
        engine.dispose()


def test_unmarked_database_write_still_discards_catalogue_index(tmp_path, monkeypatch):
    engine, factory = _factory(tmp_path)
    loads = 0

    def load(*args, **kwargs):
        nonlocal loads
        loads += 1
        return PlanIndex()

    monkeypatch.setattr(cache, "load_index", load)
    cache._CACHE.clear()
    try:
        first = cache.get_index(factory)
        with engine.begin() as connection:
            connection.execute(
                text("insert into personal_note (value) values ('unknown write')")
            )

        assert cache.get_index(factory) is not first
        assert loads == 2
    finally:
        cache._CACHE.clear()
        engine.dispose()


def test_ranking_reuses_cached_standalone_prices(tmp_path, monkeypatch):
    engine, factory = _factory(tmp_path)
    index = PlanIndex(recipes={1: object(), 2: object()})  # type: ignore[arg-type]
    scored: list[int] = []
    ranking_prices = None

    monkeypatch.setattr(cache, "load_index", lambda *args, **kwargs: index)

    def score(_index, selections, **kwargs):
        recipe_id = next(iter(selections)).recipe_id
        scored.append(recipe_id)
        return BasketScore(
            score=float(recipe_id),
            cost=float(recipe_id) + 0.5,
            consumed_cost=float(recipe_id) / 2,
            gap_count=0,
        )

    def rank(*args, standalone_prices, **kwargs):
        nonlocal ranking_prices
        ranking_prices = standalone_prices
        return []

    monkeypatch.setattr(cache, "score_basket", score)
    monkeypatch.setattr(cache, "rank_candidates", rank)
    cache._CACHE.clear()
    try:
        standalone = cache.get_standalone_prices(factory, servings=4)
        cache.get_ranking(
            factory,
            [Selection(recipe_id=1, servings=4)],
            candidate_portions=4,
        )

        assert scored == [1, 2]
        assert ranking_prices is standalone
        assert standalone[2].cost == 2.5
    finally:
        cache._CACHE.clear()
        engine.dispose()


def test_shared_line_shortcut_matches_full_basket_scoring():
    rice = Ingredient(
        key="rice",
        name="Rice",
        pantry_staple=False,
        packs=(Pack("rice-500", "Rice 500g", 500, 2.0, 0.8, 1, "exact"),),
    )
    onions = Ingredient(
        key="onion",
        name="Onions",
        pantry_staple=False,
        packs=(
            Pack(
                "onion-3",
                "Three onions",
                300,
                1.5,
                0.2,
                1,
                "exact",
                capacity_qty=3,
                quantity_unit="unit",
            ),
        ),
        unit_kind="count",
    )
    beans = Ingredient(
        key="beans",
        name="Beans",
        pantry_staple=False,
        packs=(Pack("beans-400", "Beans 400g", 400, 1.0, 0.4, 1, "exact"),),
    )
    index = PlanIndex(
        ingredients={"rice": rice, "onion": onions, "beans": beans},
        recipes={
            1: PlanRecipe(
                1,
                "Pinned",
                2,
                (Need("rice", "Rice", 300), Need("onion", "Onion", 100, units=1)),
            ),
            2: PlanRecipe(
                2,
                "Shared",
                2,
                (Need("rice", "Rice", 250), Need("onion", "Onion", 200, units=2)),
            ),
            3: PlanRecipe(3, "Separate", 2, (Need("beans", "Beans", 300),)),
        },
    )
    pinned = [Selection(recipe_id=1, servings=4)]
    standalone = {}
    for recipe_id in (2, 3):
        scored = score_basket(index, [Selection(recipe_id=recipe_id, servings=4)])
        standalone[recipe_id] = cache.StandalonePrice(
            score=scored.score,
            cost=scored.cost,
            consumed_cost=scored.consumed_cost,
            gap_count=scored.gap_count,
        )

    fast = rank_candidates(
        index,
        pinned,
        [2, 3],
        candidate_portions=4,
        standalone_prices=standalone,
    )
    reference = rank_candidates(index, pinned, [2, 3], candidate_portions=4)

    assert [candidate.recipe_id for candidate in fast] == [
        candidate.recipe_id for candidate in reference
    ]
    for candidate, expected in zip(fast, reference, strict=True):
        assert candidate.marginal == pytest.approx(expected.marginal)
        assert candidate.marginal_cost == pytest.approx(expected.marginal_cost)
        assert candidate.standalone == pytest.approx(expected.standalone)
        assert candidate.standalone_cost == pytest.approx(expected.standalone_cost)
