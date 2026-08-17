"""The cupboard: what it admits, how it decays, and what a shop leaves in it.

The pantry's whole risk is that it believes more than it knows, so most of what
is pinned here is a *limit* on belief rather than a feature: the chiller never
enters, silence shrinks a holding, an old holding disappears outright, and a
week nobody shopped for consumes nothing.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.db.models import (
    PantryLot,
    PlanCookMark,
    PlanSelection,
    PlanWeekPush,
    Recipe,
)
from app.db.session import init_db, make_engine, make_session_factory
from app.pantry import cooks, store
from app.pantry.harvest import lots_from_basket
from app.pantry.model import (
    PANTRY_MIN_SALVAGE,
    TRUST_HORIZON_CYCLES,
    Lot,
    Quantity,
    admits,
    cycles_between,
    decay,
    held,
    is_stale,
    remaining,
)
from app.planner.basket import Basket, BasketContribution, BasketLine, Cover, PackChoice
from app.planner.index import Pack
from tests.conftest import user_id

KEY = "name:basmati-rice"


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(tmp_path / "pantry.db")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def uid(factory):
    with factory() as session:
        return user_id(session)


def _recipe(session, source_id, name):
    recipe = Recipe(
        source="hellofresh", source_id=source_id, url="", name=name, curated=1, base_yield=2
    )
    session.add(recipe)
    session.commit()
    return recipe.id


# --------------------------------------------------------------------------
# What may enter the cupboard at all
# --------------------------------------------------------------------------

def test_the_chiller_and_the_bakery_never_enter():
    """The fastest-drifting stock is exactly what the waste model already zeroes.

    Excluding it is what makes the rest of the model safe: nothing whose real
    quantity could change without the app hearing about it is ever carried.
    """
    assert not admits(0.15)  # _CHILLED
    assert not admits(0.20)  # _BAKERY
    assert admits(0.85)      # _AMBIENT
    assert admits(0.90)      # frozen


def test_a_staple_is_not_carried_even_though_it_keeps():
    """Salt keeps forever, but the basket never bought it, so there is no
    remainder to carry — and its consumption is diffuse rather than plannable."""
    assert not admits(0.85, pantry_staple=True)


def test_an_unknown_salvage_is_refused_rather_than_guessed():
    assert not admits(None)


def test_the_threshold_sits_between_the_chiller_and_the_cupboard():
    assert 0.20 < PANTRY_MIN_SALVAGE < 0.85


# --------------------------------------------------------------------------
# Decay: per shop, and with an end
# --------------------------------------------------------------------------

def test_decay_counts_shops_not_weeks():
    """A fortnightly shopper's leftovers survive one shop between shops, not two."""
    assert cycles_between("2026-01-05", "2026-01-19", cadence_weeks=1) == 2
    assert cycles_between("2026-01-05", "2026-01-19", cadence_weeks=2) == 1


def test_decay_never_runs_backwards():
    assert cycles_between("2026-01-19", "2026-01-05", cadence_weeks=1) == 0


def test_each_shop_shrinks_a_holding():
    quantity = Quantity(grams=1000.0)
    one = decay(quantity, salvage=0.85, cycles=1)
    two = decay(quantity, salvage=0.85, cycles=2)
    assert one.grams == pytest.approx(850.0)
    assert two.grams == pytest.approx(722.5)


def test_a_holding_past_the_trust_horizon_is_dropped_not_shrunk():
    """The point of the horizon: one 5 kg sack of flour must not suppress flour
    purchases forever at a slowly shrinking figure."""
    aged = decay(Quantity(grams=5000.0), salvage=0.9, cycles=TRUST_HORIZON_CYCLES)
    assert aged.grams == 0.0


def test_a_stale_lot_is_stale_regardless_of_how_much_it_held():
    lot = Lot(
        ingredient_key=KEY,
        ingredient_name="Basmati Rice",
        week_start="2026-01-05",
        available=Quantity(grams=5000.0),
        salvage=0.9,
        contributions={},
    )
    long_after = date(2026, 6, 1)
    assert is_stale(lot, today=long_after, cadence_weeks=1)
    assert not is_stale(lot, today=date(2026, 1, 12), cadence_weeks=1)


def test_a_confirmation_restarts_the_clock():
    """"Yes, that is still there" is the best evidence the pantry ever gets, so
    it outranks the guess it replaces rather than merely nudging it."""
    common = dict(
        ingredient_key=KEY,
        ingredient_name="Basmati Rice",
        week_start="2026-01-05",
        available=Quantity(grams=1000.0),
        salvage=0.85,
        contributions={},
    )
    unconfirmed = Lot(**common)
    confirmed = Lot(**common, confirmed_week_start="2026-02-02")
    target = "2026-02-09"
    stale = held(unconfirmed, cooked_recipe_ids=set(), target_week=target, cadence_weeks=1)
    fresh = held(confirmed, cooked_recipe_ids=set(), target_week=target, cadence_weeks=1)
    assert fresh.grams > stale.grams


# --------------------------------------------------------------------------
# Consumption is derived, so an untick puts the grams back
# --------------------------------------------------------------------------

def test_cooking_a_recipe_takes_its_share_and_unticking_returns_it():
    contributions = {1: Quantity(grams=150.0), 2: Quantity(grams=250.0)}
    available = Quantity(grams=1000.0)
    both = remaining(available=available, contributions=contributions, cooked_recipe_ids={1, 2})
    one = remaining(available=available, contributions=contributions, cooked_recipe_ids={1})
    none = remaining(available=available, contributions=contributions, cooked_recipe_ids=set())
    assert both.grams == pytest.approx(600.0)
    assert one.grams == pytest.approx(850.0)
    assert none.grams == pytest.approx(1000.0)


def test_a_lot_never_goes_negative_when_the_week_over_consumed_it():
    left = remaining(
        available=Quantity(grams=100.0),
        contributions={1: Quantity(grams=500.0)},
        cooked_recipe_ids={1},
    )
    assert left.grams == 0.0


def test_running_out_empties_a_lot_outright():
    lot = Lot(
        ingredient_key=KEY,
        ingredient_name="Basmati Rice",
        week_start="2026-01-05",
        available=Quantity(grams=1000.0),
        salvage=0.85,
        contributions={},
        emptied=True,
    )
    assert not held(lot, cooked_recipe_ids=set(), target_week="2026-01-12", cadence_weeks=1)


# --------------------------------------------------------------------------
# Cooked-ness: optimistic, but gated on the shop having happened
# --------------------------------------------------------------------------

def test_a_finished_shopped_week_is_assumed_cooked(factory, uid):
    with factory() as session:
        rid = _recipe(session, "r1", "Rice Bowl")
        session.add(PlanSelection(user_id=uid, week_start="2026-01-05", recipe_id=rid))
        session.add(
            PlanWeekPush(user_id=uid, retailer="ocado", week_start="2026-01-05")
        )
        session.commit()
        assert cooks.cooked_recipe_ids(
            session, uid, "2026-01-05", today=date(2026, 1, 20)
        ) == {rid}


def test_an_unshopped_week_consumes_nothing(factory, uid):
    """The guard against an idle fortnight quietly emptying a cupboard that was
    never filled: a week planned and then abandoned pushes nothing."""
    with factory() as session:
        rid = _recipe(session, "r1", "Rice Bowl")
        session.add(PlanSelection(user_id=uid, week_start="2026-01-05", recipe_id=rid))
        session.commit()
        assert cooks.cooked_recipe_ids(
            session, uid, "2026-01-05", today=date(2026, 1, 20)
        ) == set()


def test_a_week_still_under_way_has_cooked_nothing_yet(factory, uid):
    with factory() as session:
        rid = _recipe(session, "r1", "Rice Bowl")
        session.add(PlanSelection(user_id=uid, week_start="2026-01-05", recipe_id=rid))
        session.add(PlanWeekPush(user_id=uid, retailer="ocado", week_start="2026-01-05"))
        session.commit()
        assert cooks.cooked_recipe_ids(
            session, uid, "2026-01-05", today=date(2026, 1, 7)
        ) == set()


def test_a_mark_overrides_the_assumption_in_both_directions(factory, uid):
    with factory() as session:
        cooked = _recipe(session, "r1", "Rice Bowl")
        skipped = _recipe(session, "r2", "Chilli")
        for rid in (cooked, skipped):
            session.add(PlanSelection(user_id=uid, week_start="2026-01-05", recipe_id=rid))
        session.add(PlanWeekPush(user_id=uid, retailer="ocado", week_start="2026-01-05"))
        # "I did not make the chilli" against a week that assumes both were made.
        session.add(
            PlanCookMark(
                user_id=uid, week_start="2026-01-05", recipe_id=skipped, cooked=False
            )
        )
        session.commit()
        assert cooks.cooked_recipe_ids(
            session, uid, "2026-01-05", today=date(2026, 1, 20)
        ) == {cooked}


# --------------------------------------------------------------------------
# Harvesting a pushed basket
# --------------------------------------------------------------------------

def _line(key, name, *, capacity_g, need_g, salvage, contributions=(), unit_kind="mass"):
    chosen = Pack(
        sku="p1", product_name=name, capacity_g=capacity_g, price=2.0, salvage=salvage,
        rank=1, match_type="exact", pack_size_raw=f"{capacity_g:g}g",
    )
    cover = Cover(
        choices=(PackChoice(pack=chosen, count=1),),
        need_g=need_g,
        capacity_g=capacity_g,
        cost=2.0,
        leftover_g=max(0.0, capacity_g - need_g),
        waste_gbp=0.0,
        salvage=salvage,
    )
    return BasketLine(
        key=key, name=name, need_g=need_g, cover=cover, unit_kind=unit_kind,
        contributions=tuple(
            BasketContribution(
                recipe_id=rid, recipe_name=f"r{rid}", grams=grams, quantity=None,
                quantity_unit="g",
            )
            for rid, grams in contributions
        ),
    )


def test_a_push_leaves_what_it_bought_plus_what_it_carried_in():
    """``available`` is the shelf after the shop, so an earlier holding that the
    build did not spend is still on it."""
    basket = Basket(lines=[_line(KEY, "Rice", capacity_g=1000, need_g=300, salvage=0.85)])
    lots = lots_from_basket(
        basket, held={KEY: Quantity(grams=200.0)}, prior_salvage={KEY: 0.85}
    )
    assert len(lots) == 1
    assert lots[0].available.grams == pytest.approx(1200.0)


def test_a_chilled_line_is_not_harvested():
    basket = Basket(lines=[_line("name:chicken", "Chicken", capacity_g=500, need_g=400, salvage=0.15)])
    assert lots_from_basket(basket, held={}, prior_salvage={}) == []


def test_an_owned_line_is_not_harvested():
    """"I already have it" says nothing about how much, and a quantity the model
    cannot state is one it must not carry."""
    basket = Basket(lines=[_line(KEY, "Rice", capacity_g=1000, need_g=300, salvage=0.85)])
    assert lots_from_basket(
        basket, held={}, prior_salvage={}, owned_item_keys={KEY}
    ) == []


def test_a_line_met_wholly_from_the_cupboard_keeps_its_old_salvage():
    """It bought nothing, so there is no cover to read a salvage from — the
    honest figure is the one the food came in with."""
    basket = Basket(
        lines=[BasketLine(key=KEY, name="Rice", need_g=0.0, pantry_g=300.0, note="in the cupboard")]
    )
    lots = lots_from_basket(
        basket, held={KEY: Quantity(grams=700.0)}, prior_salvage={KEY: 0.85}
    )
    assert len(lots) == 1
    assert lots[0].salvage == pytest.approx(0.85)
    assert lots[0].available.grams == pytest.approx(700.0)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _deposit(factory, uid, week_start, grams, *, contributions=None, salvage=0.85):
    store.deposit(
        factory,
        [
            Lot(
                ingredient_key=KEY,
                ingredient_name="Basmati Rice",
                week_start=week_start,
                available=Quantity(grams=grams),
                salvage=salvage,
                contributions=contributions or {},
            )
        ],
        user_id=uid,
        retailer="ocado",
        week_start=week_start,
        today=date(2026, 1, 6),
    )


def test_the_cupboard_is_read_from_before_the_week_being_shopped_for(factory, uid):
    """A week's own deposit describes the shelf *after* its shop. Reading it back
    while shopping for that same week would count the shop twice — which is what
    makes a re-push of one week idempotent rather than compounding."""
    _deposit(factory, uid, "2026-01-05", 1000.0)
    same_week = store.read_pantry(
        factory, user_id=uid, retailer="ocado", target_week="2026-01-05",
        today=date(2026, 1, 6),
    )
    next_week = store.read_pantry(
        factory, user_id=uid, retailer="ocado", target_week="2026-01-12",
        today=date(2026, 1, 6),
    )
    assert same_week == {}
    assert next_week[KEY].grams == pytest.approx(850.0)  # one shop of decay


def test_a_later_shop_shadows_the_earlier_one_rather_than_stacking(factory, uid):
    _deposit(factory, uid, "2026-01-05", 1000.0)
    _deposit(factory, uid, "2026-01-12", 1200.0)
    read = store.read_pantry(
        factory, user_id=uid, retailer="ocado", target_week="2026-01-19",
        today=date(2026, 1, 13),
    )
    # 1200 decayed one shop, not 1200 + 1000.
    assert read[KEY].grams == pytest.approx(1020.0)


def test_re_pushing_the_same_week_does_not_stack_a_second_shop(factory, uid):
    _deposit(factory, uid, "2026-01-05", 1000.0)
    _deposit(factory, uid, "2026-01-05", 1000.0)
    with factory() as session:
        rows = session.scalars(select(PantryLot)).all()
    assert len(rows) == 1
    assert rows[0].available_g == pytest.approx(1000.0)


def test_unticking_a_recipe_returns_its_grams_to_the_next_shop(factory, uid):
    with factory() as session:
        rid = _recipe(session, "r1", "Rice Bowl")
        session.add(PlanSelection(user_id=uid, week_start="2026-01-05", recipe_id=rid))
        session.add(PlanWeekPush(user_id=uid, retailer="ocado", week_start="2026-01-05"))
        session.commit()
    _deposit(factory, uid, "2026-01-05", 1000.0, contributions={rid: Quantity(grams=300.0)})

    after_cooking = store.read_pantry(
        factory, user_id=uid, retailer="ocado", target_week="2026-01-12",
        today=date(2026, 1, 20),
    )
    with factory() as session:
        session.add(
            PlanCookMark(user_id=uid, week_start="2026-01-05", recipe_id=rid, cooked=False)
        )
        session.commit()
    after_unticking = store.read_pantry(
        factory, user_id=uid, retailer="ocado", target_week="2026-01-12",
        today=date(2026, 1, 20),
    )
    assert after_cooking[KEY].grams == pytest.approx(595.0)   # (1000-300) * 0.85
    assert after_unticking[KEY].grams == pytest.approx(850.0)  # 1000 * 0.85


def test_running_out_is_believed_and_confirming_restores_the_clock(factory, uid):
    _deposit(factory, uid, "2026-01-05", 1000.0)
    assert store.empty(factory, user_id=uid, retailer="ocado", ingredient_key=KEY)
    assert store.read_pantry(
        factory, user_id=uid, retailer="ocado", target_week="2026-01-12",
        today=date(2026, 1, 6),
    ) == {}

    assert store.confirm(
        factory, user_id=uid, retailer="ocado", ingredient_key=KEY,
        week_start="2026-01-12",
    )
    restored = store.read_pantry(
        factory, user_id=uid, retailer="ocado", target_week="2026-01-12",
        today=date(2026, 1, 13),
    )
    # Confirmed in the target week itself, so nothing has decayed off it.
    assert restored[KEY].grams == pytest.approx(1000.0)


def test_correcting_an_ingredient_with_nothing_held_reports_no_change(factory, uid):
    assert not store.empty(
        factory, user_id=uid, retailer="ocado", ingredient_key="name:nothing"
    )


def test_a_stale_lot_is_pruned_by_the_next_deposit(factory, uid):
    _deposit(factory, uid, "2026-01-05", 1000.0)
    store.deposit(
        factory,
        [
            Lot(
                ingredient_key="name:lentils",
                ingredient_name="Lentils",
                week_start="2026-06-01",
                available=Quantity(grams=500.0),
                salvage=0.85,
                contributions={},
            )
        ],
        user_id=uid,
        retailer="ocado",
        week_start="2026-06-01",
        today=date(2026, 6, 2),
    )
    with factory() as session:
        keys = set(session.scalars(select(PantryLot.ingredient_key)))
    assert keys == {"name:lentils"}


def test_one_users_cupboard_is_not_anothers(factory, uid):
    _deposit(factory, uid, "2026-01-05", 1000.0)
    read = store.read_pantry(
        factory, user_id=uid + 1, retailer="ocado", target_week="2026-01-12",
        today=date(2026, 1, 6),
    )
    assert read == {}


def test_one_shops_cupboard_is_not_anothers(factory, uid):
    """Pack sizes differ per shop, so a remainder belongs to the basket it came
    out of rather than to the household at large."""
    _deposit(factory, uid, "2026-01-05", 1000.0)
    read = store.read_pantry(
        factory, user_id=uid, retailer="sainsburys", target_week="2026-01-12",
        today=date(2026, 1, 6),
    )
    assert read == {}
