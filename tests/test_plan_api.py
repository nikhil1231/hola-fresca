"""The plan on the server: per-week recipes, per-week basket decisions, import.

What these pin is the point of moving the plan off the device — that it survives,
that two clients editing it do not overwrite each other, and that what comes back
is the library's current view of a recipe rather than a snapshot taken whenever
it was added.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import schedule as sched
from app.api.deps import get_session
from app.db.models import PlanSelection, PlanWeekItem, PlanWeekPush, Recipe, User
from app.db.session import init_db, make_engine, make_session_factory
from main import app
from tests.conftest import user_id


def _recipe(source_id: str, name: str, **overrides) -> Recipe:
    defaults = dict(
        source="hellofresh",
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        name=name,
        curated=1,
        is_complete=1,
        base_yield=2,
        energy_kcal=600.0,
        protein_g=40.0,
    )
    defaults.update(overrides)
    return Recipe(**defaults)


@pytest.fixture
def plan_client(tmp_path):
    engine = make_engine(tmp_path / "plan.db")
    init_db(engine)
    factory = make_session_factory(engine)

    with factory() as session:
        session.add_all(
            [
                _recipe("a", "Chicken Curry"),
                _recipe("b", "Pork Noodles"),
                _recipe("c", "Broken Row", manually_excluded=1),
                _recipe("d", "Not Curated", curated=0),
            ]
        )
        session.commit()

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()


def _week(offset_weeks: int = 0) -> str:
    return sched.format_date(sched.upcoming_week_start() + timedelta(weeks=offset_weeks))


def _recipe_id(factory, name: str) -> int:
    with factory() as session:
        return session.scalar(select(Recipe.id).where(Recipe.name == name))


# --- the basics --------------------------------------------------------------

def test_a_plan_starts_empty(plan_client):
    client, _ = plan_client
    assert client.get("/api/plan").json() == {"weeks": []}


def test_a_recipe_added_to_a_week_comes_back_as_a_card(plan_client):
    """Not as an id, and not as whatever the browser cached when it was added."""
    client, factory = plan_client
    rid = _recipe_id(factory, "Chicken Curry")
    week = _week()

    response = client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": rid})

    assert response.status_code == 201
    (entry,) = response.json()["recipes"]
    assert entry["recipe"]["name"] == "Chicken Curry"
    assert entry["recipe"]["energy_kcal"] == 600.0
    assert entry["portions"] == 4


def test_a_renamed_recipe_is_renamed_in_the_plan(plan_client):
    """The regression the snapshot caused: a plan showing a stale card for ever."""
    client, factory = plan_client
    rid = _recipe_id(factory, "Chicken Curry")
    week = _week()
    client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": rid})

    with factory() as session:
        session.get(Recipe, rid).name = "Chicken Curry (v2)"
        session.commit()

    (entry,) = client.get(f"/api/plan/weeks/{week}").json()["recipes"]
    assert entry["recipe"]["name"] == "Chicken Curry (v2)"


def test_portions_and_protein_are_patched_not_replaced(plan_client):
    client, factory = plan_client
    rid = _recipe_id(factory, "Chicken Curry")
    week = _week()
    client.post(
        f"/api/plan/weeks/{week}/recipes",
        json={"recipe_id": rid, "portions": 2, "protein": {"scale": 1.5}},
    )

    client.patch(f"/api/plan/weeks/{week}/recipes/{rid}", json={"portions": 6})

    (entry,) = client.get(f"/api/plan/weeks/{week}").json()["recipes"]
    assert entry["portions"] == 6
    assert entry["protein"]["scale"] == 1.5, "untouched by a portions-only patch"


def test_a_protein_modifier_can_be_cleared(plan_client):
    """`protein: null` has to mean something different from leaving it out."""
    client, factory = plan_client
    rid = _recipe_id(factory, "Chicken Curry")
    week = _week()
    client.post(
        f"/api/plan/weeks/{week}/recipes",
        json={"recipe_id": rid, "protein": {"swap_to": "name:tofu"}},
    )

    client.patch(f"/api/plan/weeks/{week}/recipes/{rid}", json={"protein": None})

    (entry,) = client.get(f"/api/plan/weeks/{week}").json()["recipes"]
    assert entry["protein"] is None


def test_removing_a_recipe_leaves_the_rest_of_the_week(plan_client):
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    noodles = _recipe_id(factory, "Pork Noodles")
    week = _week()
    client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": curry})
    client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": noodles})

    body = client.delete(f"/api/plan/weeks/{week}/recipes/{curry}").json()

    assert [e["recipe"]["name"] for e in body["recipes"]] == ["Pork Noodles"]


def test_weeks_are_kept_apart(plan_client):
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    client.post(f"/api/plan/weeks/{_week()}/recipes", json={"recipe_id": curry})
    client.post(f"/api/plan/weeks/{_week(1)}/recipes", json={"recipe_id": curry})

    client.delete(f"/api/plan/weeks/{_week()}/recipes/{curry}")

    assert client.get(f"/api/plan/weeks/{_week()}").json()["recipes"] == []
    assert len(client.get(f"/api/plan/weeks/{_week(1)}").json()["recipes"]) == 1


# --- concurrency: the reason writes are per row ------------------------------

def test_adding_the_same_recipe_twice_is_not_an_error(plan_client):
    """Two devices doing the same thing is not a conflict, it is agreement."""
    client, factory = plan_client
    rid = _recipe_id(factory, "Chicken Curry")
    week = _week()
    client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": rid})

    second = client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": rid})

    assert second.status_code == 201
    assert len(second.json()["recipes"]) == 1


def test_removing_something_already_gone_is_not_an_error(plan_client):
    client, factory = plan_client
    rid = _recipe_id(factory, "Chicken Curry")
    assert client.delete(f"/api/plan/weeks/{_week()}/recipes/{rid}").status_code == 200


def test_two_clients_editing_different_entries_both_win(plan_client):
    """The whole reason there is no "save the plan" endpoint."""
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    noodles = _recipe_id(factory, "Pork Noodles")
    week = _week()
    client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": curry, "portions": 4})
    client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": noodles, "portions": 4})

    # Both clients read the week, then each changes a different recipe.
    client.patch(f"/api/plan/weeks/{week}/recipes/{curry}", json={"portions": 2})
    client.patch(f"/api/plan/weeks/{week}/recipes/{noodles}", json={"portions": 6})

    portions = {
        e["recipe"]["id"]: e["portions"]
        for e in client.get(f"/api/plan/weeks/{week}").json()["recipes"]
    }
    assert portions == {curry: 2, noodles: 6}


# --- validation --------------------------------------------------------------

def test_only_mondays_are_weeks(plan_client):
    client, factory = plan_client
    rid = _recipe_id(factory, "Chicken Curry")
    tuesday = sched.format_date(sched.upcoming_week_start() + timedelta(days=1))
    assert client.post(f"/api/plan/weeks/{tuesday}/recipes", json={"recipe_id": rid}).status_code == 400
    assert client.post("/api/plan/weeks/nonsense/recipes", json={"recipe_id": rid}).status_code == 400


def test_recipes_outside_the_library_cannot_be_planned(plan_client):
    client, factory = plan_client
    week = _week()
    for name in ("Broken Row", "Not Curated"):
        rid = _recipe_id(factory, name)
        assert client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": rid}).status_code == 404
    assert client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": 9999}).status_code == 404


def test_a_week_has_a_ceiling(plan_client):
    """Well above any sane setting: it exists so storage cannot run away."""
    client, factory = plan_client
    week = _week()
    with factory() as session:
        session.add_all([_recipe(f"bulk{i}", f"Bulk {i}") for i in range(20)])
        session.commit()
        ids = list(session.scalars(select(Recipe.id).where(Recipe.name.like("Bulk %"))))

    codes = [
        client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": rid}).status_code
        for rid in ids
    ]
    assert codes.count(201) == 14
    assert codes.count(400) == 6


# --- per-week basket decisions ----------------------------------------------

def test_week_items_carry_pack_snap_and_owned(plan_client):
    client, _ = plan_client
    week = _week()

    client.put(f"/api/plan/weeks/{week}/items/name:rice", json={"pack_sku": "1kg"})
    client.put(f"/api/plan/weeks/{week}/items/name:rice", json={"snapped": True})
    client.put(f"/api/plan/weeks/{week}/items/name:salt", json={"owned": True})

    body = client.get(f"/api/plan/weeks/{week}").json()
    assert body["pack_overrides"] == {"name:rice": "1kg"}
    assert body["snap_overrides"] == {"name:rice": True}
    assert body["owned_item_keys"] == ["name:salt"]


def test_setting_one_field_leaves_the_others(plan_client):
    """The basket page ticks "already own this" without knowing the pack."""
    client, _ = plan_client
    week = _week()
    client.put(f"/api/plan/weeks/{week}/items/name:rice", json={"pack_sku": "1kg"})

    client.put(f"/api/plan/weeks/{week}/items/name:rice", json={"owned": True})

    body = client.get(f"/api/plan/weeks/{week}").json()
    assert body["pack_overrides"] == {"name:rice": "1kg"}
    assert body["owned_item_keys"] == ["name:rice"]


def test_an_item_saying_nothing_is_deleted_rather_than_stored(plan_client):
    client, factory = plan_client
    week = _week()
    client.put(f"/api/plan/weeks/{week}/items/name:rice", json={"pack_sku": "1kg"})

    client.put(f"/api/plan/weeks/{week}/items/name:rice", json={"pack_sku": None})

    with factory() as session:
        assert session.scalar(select(PlanWeekItem.id)) is None


def test_clearing_a_week_takes_its_item_decisions_with_it(plan_client):
    """Otherwise a week rebuilt later comes back wearing last time's pack choices."""
    client, factory = plan_client
    rid = _recipe_id(factory, "Chicken Curry")
    week = _week()
    client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": rid})
    client.put(f"/api/plan/weeks/{week}/items/name:rice", json={"pack_sku": "1kg"})

    client.delete(f"/api/plan/weeks/{week}")

    with factory() as session:
        assert session.scalar(select(PlanSelection.id)) is None
        assert session.scalar(select(PlanWeekItem.id)) is None


# --- the past is a record, not a draft ---------------------------------------

def _seed_past_week(factory, recipe_id: int, weeks_back: int = 3) -> str:
    """A week that has been and gone, planned and shopped for.

    Written straight to the database because the API will not accept it, which is
    the whole point: a real one got there weeks ago, when it was still the week
    being planned.
    """
    week = sched.format_date(sched.upcoming_week_start() - timedelta(weeks=weeks_back))
    with factory() as session:
        session.add(
            PlanSelection(
                user_id=user_id(session),
                week_start=week,
                recipe_id=recipe_id,
                position=0,
                portions=4,
                protein_json='{"scale": 1.5}',
            )
        )
        session.add(
            PlanWeekItem(
                user_id=user_id(session),
                week_start=week,
                ingredient_key="name:rice",
                pack_sku="1kg",
                snapped=True,
            )
        )
        session.commit()
    return week


def test_a_finished_week_still_says_how_it_was_cooked(plan_client):
    """The complaint this exists for: last week's 1.5x protein, still there."""
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    week = _seed_past_week(factory, curry)

    body = client.get(f"/api/plan/weeks/{week}").json()

    (entry,) = body["recipes"]
    assert entry["protein"]["scale"] == 1.5
    assert body["pack_overrides"] == {"name:rice": "1kg"}
    assert body["snap_overrides"] == {"name:rice": True}


def test_the_whole_plan_reads_back_as_far_as_it_goes(plan_client):
    """Reads are not trimmed to the planning window — only writes are."""
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    past = _seed_past_week(factory, curry)
    client.post(f"/api/plan/weeks/{_week()}/recipes", json={"recipe_id": curry})

    weeks = [week["week_start"] for week in client.get("/api/plan").json()["weeks"]]

    assert weeks == sorted([past, _week()])


def test_a_finished_week_refuses_every_write(plan_client):
    """Hiding the buttons is the UI's job; this is what makes it true."""
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    noodles = _recipe_id(factory, "Pork Noodles")
    week = _seed_past_week(factory, curry)

    attempts = [
        client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": noodles}),
        client.patch(f"/api/plan/weeks/{week}/recipes/{curry}", json={"portions": 8}),
        client.patch(f"/api/plan/weeks/{week}/recipes/{curry}", json={"protein": None}),
        client.delete(f"/api/plan/weeks/{week}/recipes/{curry}"),
        client.put(f"/api/plan/weeks/{week}/items/name:rice", json={"owned": True}),
        client.delete(f"/api/plan/weeks/{week}"),
    ]

    assert [attempt.status_code for attempt in attempts] == [409] * len(attempts)
    body = client.get(f"/api/plan/weeks/{week}").json()
    assert [entry["recipe"]["id"] for entry in body["recipes"]] == [curry]
    assert body["recipes"][0]["portions"] == 4
    assert body["recipes"][0]["protein"]["scale"] == 1.5
    assert body["pack_overrides"] == {"name:rice": "1kg"}


def test_the_week_being_planned_is_still_writable(plan_client):
    """The boundary is the upcoming week itself, which is not past."""
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    assert (
        client.post(f"/api/plan/weeks/{_week()}/recipes", json={"recipe_id": curry}).status_code
        == 201
    )


# --- the one-off import from localStorage ------------------------------------

def test_import_takes_on_a_plan_from_a_browser(plan_client):
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    week = _week()

    body = client.post(
        "/api/plan/import",
        json={
            "weeks": [
                {
                    "week_start": week,
                    "recipes": [{"recipe_id": curry, "portions": 2}],
                    "pack_overrides": {"name:rice": "1kg"},
                    "snap_overrides": {"name:rice": True},
                    "owned_item_keys": ["name:salt"],
                }
            ]
        },
    ).json()

    assert body["imported_weeks"] == 1
    assert body["imported_recipes"] == 1
    (imported,) = body["plan"]["weeks"]
    assert imported["recipes"][0]["portions"] == 2
    assert imported["pack_overrides"] == {"name:rice": "1kg"}
    assert imported["owned_item_keys"] == ["name:salt"]


def test_import_still_takes_on_weeks_that_have_been_and_gone(plan_client):
    """The read-only rule is about editing history, not about receiving it.

    A plan carried over from a browser is mostly history by definition — that is
    where the record of how those weeks were cooked has been living.
    """
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    week = sched.format_date(sched.upcoming_week_start() - timedelta(weeks=2))

    body = client.post(
        "/api/plan/import",
        json={
            "weeks": [
                {
                    "week_start": week,
                    "recipes": [{"recipe_id": curry, "protein": {"scale": 1.5}}],
                }
            ]
        },
    ).json()

    assert body["imported_weeks"] == 1
    (imported,) = body["plan"]["weeks"]
    assert imported["week_start"] == week
    assert imported["recipes"][0]["protein"]["scale"] == 1.5


def test_import_never_overwrites_a_week_that_has_recipes(plan_client):
    """It runs unattended on first load; it must not be able to destroy a plan."""
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    noodles = _recipe_id(factory, "Pork Noodles")
    week = _week()
    client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": curry})

    body = client.post(
        "/api/plan/import",
        json={"weeks": [{"week_start": week, "recipes": [{"recipe_id": noodles}]}]},
    ).json()

    assert body["imported_weeks"] == 0
    (kept,) = body["plan"]["weeks"]
    assert [e["recipe"]["name"] for e in kept["recipes"]] == ["Chicken Curry"]


def test_import_skips_recipes_that_have_left_the_library(plan_client):
    """A two-year-old localStorage plan will name recipes that are gone."""
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    gone = _recipe_id(factory, "Broken Row")
    week = _week()

    body = client.post(
        "/api/plan/import",
        json={
            "weeks": [
                {
                    "week_start": week,
                    "recipes": [{"recipe_id": curry}, {"recipe_id": gone}],
                }
            ]
        },
    ).json()

    assert body["imported_recipes"] == 1
    assert body["skipped_recipes"] == [gone]


# --- ownership ---------------------------------------------------------------

def test_a_plan_belongs_to_one_account(plan_client):
    """There is one user today, so this is checked below the API.

    It is the invariant the whole migration exists for, though: every read is
    filtered by user_id, so a second account's week is not visible from the
    first even when both name the same week.
    """
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    week = _week()
    client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": curry})

    with factory() as session:
        other = User(name="Someone Else")
        session.add(other)
        session.commit()
        session.add(
            PlanSelection(
                user_id=other.id, week_start=week, recipe_id=curry, portions=8
            )
        )
        session.commit()
        mine = user_id(session)
        assert other.id != mine

    (entry,) = client.get(f"/api/plan/weeks/{week}").json()["recipes"]
    assert entry["portions"] == 4, "the other account's row is not mine"


# --- what actually got cooked ------------------------------------------------

def _mark_shopped(factory, week: str) -> None:
    """The evidence the optimistic default is gated on: a real push happened."""
    with factory() as session:
        session.add(
            PlanWeekPush(user_id=user_id(session), retailer="ocado", week_start=week)
        )
        session.commit()


def test_a_shopped_week_that_has_ended_reads_as_cooked(plan_client):
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    week = _seed_past_week(factory, curry)
    _mark_shopped(factory, week)

    body = client.get("/api/plan/cooked", params={"week_start": week}).json()
    assert body["weeks"][0]["shopped"] is True
    assert body["weeks"][0]["recipes"] == [
        {"recipe_id": curry, "cooked": True, "marked": False}
    ]


def test_a_week_never_pushed_to_a_cart_cooked_nothing(plan_client):
    """Otherwise an idle fortnight quietly empties a cupboard nobody filled."""
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    week = _seed_past_week(factory, curry)

    body = client.get("/api/plan/cooked", params={"week_start": week}).json()
    assert body["weeks"][0]["shopped"] is False
    assert body["weeks"][0]["recipes"][0]["cooked"] is False


def test_unticking_a_recipe_survives_and_is_marked_as_yours(plan_client):
    """The assumption moves when a week ends, so the statement is stored
    outright rather than only when it currently differs."""
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    week = _seed_past_week(factory, curry)
    _mark_shopped(factory, week)

    response = client.put(f"/api/plan/weeks/{week}/cooked/{curry}", json={"cooked": False})
    assert response.status_code == 200
    assert response.json()["recipes"] == [
        {"recipe_id": curry, "cooked": False, "marked": True}
    ]

    again = client.get("/api/plan/cooked", params={"week_start": week}).json()
    assert again["weeks"][0]["recipes"][0]["cooked"] is False


def test_a_recipe_can_be_ticked_back_on(plan_client):
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    week = _seed_past_week(factory, curry)
    _mark_shopped(factory, week)

    client.put(f"/api/plan/weeks/{week}/cooked/{curry}", json={"cooked": False})
    response = client.put(f"/api/plan/weeks/{week}/cooked/{curry}", json={"cooked": True})
    assert response.json()["recipes"][0] == {
        "recipe_id": curry, "cooked": True, "marked": True
    }


def test_a_week_that_has_not_started_cannot_have_cooked_anything(plan_client):
    """Unlike the rest of the plan API, the past is writable here — the future
    is what is refused."""
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    week = _week(2)
    client.post(f"/api/plan/weeks/{week}/recipes", json={"recipe_id": curry})

    response = client.put(f"/api/plan/weeks/{week}/cooked/{curry}", json={"cooked": True})
    assert response.status_code == 409


def test_a_recipe_outside_the_week_cannot_be_marked(plan_client):
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    noodles = _recipe_id(factory, "Pork Noodles")
    week = _seed_past_week(factory, curry)

    response = client.put(f"/api/plan/weeks/{week}/cooked/{noodles}", json={"cooked": False})
    assert response.status_code == 404


def test_cooked_asks_for_the_weeks_it_wants(plan_client):
    client, factory = plan_client
    curry = _recipe_id(factory, "Chicken Curry")
    older = _seed_past_week(factory, curry, weeks_back=5)
    newer = _seed_past_week(factory, curry, weeks_back=2)

    body = client.get(
        "/api/plan/cooked", params={"week_start": [older, newer]}
    ).json()
    assert [week["week_start"] for week in body["weeks"]] == [older, newer]
