"""Shopping schedule: cadence arithmetic, cutoffs, skips and pausing."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import schedule as sched
from app.api.deps import get_session
from tests.conftest import user_id
from app.db.models import PlanWeek
from app.db.session import init_db, make_engine, make_session_factory
from main import app


@pytest.fixture
def schedule_client(tmp_path):
    engine = make_engine(tmp_path / "schedule.db")
    init_db(engine)
    factory = make_session_factory(engine)

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()


def _next_monday(today: date | None = None) -> date:
    return sched.upcoming_week_start(today)


# --- domain ------------------------------------------------------------------

def test_upcoming_week_start_keeps_todays_week_on_monday():
    monday = date(2026, 8, 3)
    assert sched.upcoming_week_start(monday) == monday
    assert sched.upcoming_week_start(date(2026, 8, 4)) == date(2026, 8, 10)
    assert sched.upcoming_week_start(date(2026, 8, 9)) == date(2026, 8, 10)


def test_cycle_keeps_its_phase_when_the_anchor_is_in_the_past():
    # Fortnightly from an anchor months back: the cycle must still land on the
    # same alternate weeks, not re-base on today.
    anchor = date(2026, 3, 2)
    weeks = sched.cycle_week_starts(
        anchor, cadence_weeks=2, count=3, today=date(2026, 8, 4)
    )
    assert weeks == [date(2026, 8, 17), date(2026, 8, 31), date(2026, 9, 14)]
    assert all((week - anchor).days % 14 == 0 for week in weeks)


def test_cycle_starts_at_the_anchor_when_it_is_still_ahead():
    anchor = date(2026, 9, 7)
    weeks = sched.cycle_week_starts(
        anchor, cadence_weeks=1, count=2, today=date(2026, 8, 4)
    )
    assert weeks == [date(2026, 9, 7), date(2026, 9, 14)]


def test_past_weeks_step_back_from_the_planning_window():
    # Monday: today's week is in the window, so history starts the week before.
    assert sched.past_week_starts(
        date(2026, 8, 3), cadence_weeks=1, count=3, today=date(2026, 8, 3)
    ) == [date(2026, 7, 13), date(2026, 7, 20), date(2026, 7, 27)]

    # Tuesday: the window has moved to next Monday, so the week being cooked
    # right now is the most recent past week rather than falling off the page.
    assert sched.past_week_starts(
        date(2026, 8, 3), cadence_weeks=1, count=2, today=date(2026, 8, 4)
    ) == [date(2026, 7, 27), date(2026, 8, 3)]


def test_past_weeks_keep_the_cadence_and_skip_weeks_that_have_not_happened():
    # Anchor five weeks out: the fortnights between now and it are on the cadence
    # but still ahead, so they are neither planned weeks nor past ones.
    past = sched.past_week_starts(
        date(2026, 9, 7), cadence_weeks=2, count=2, today=date(2026, 8, 4)
    )
    assert past == [date(2026, 7, 13), date(2026, 7, 27)]
    assert all(week <= date(2026, 8, 4) for week in past)


def test_a_week_is_complete_once_its_last_day_has_passed():
    assert not sched.is_complete(date(2026, 8, 3), date(2026, 8, 9))
    assert sched.is_complete(date(2026, 8, 3), date(2026, 8, 10))


def test_cutoff_is_days_before_the_week_at_the_stated_time():
    assert sched.cutoff_at(
        date(2026, 8, 10), days_before=2, at=time(18, 0)
    ) == datetime(2026, 8, 8, 18, 0)


def test_active_week_is_the_first_one_still_open():
    weeks = [date(2026, 8, 10), date(2026, 8, 17), date(2026, 8, 24)]
    built = sched.build_weeks(
        weeks,
        skipped={"2026-08-17"},
        cutoff_days_before=2,
        cutoff_time=time(18, 0),
        paused=False,
        # Past the first week's Saturday cutoff, so it is closed; the second is
        # skipped; the third is the one to plan.
        now=datetime(2026, 8, 9, 9, 0),
    )
    assert [week.status for week in built] == ["closed", "skipped", "open"]
    assert [week.is_active for week in built] == [False, False, True]


def test_pausing_leaves_no_week_active():
    built = sched.build_weeks(
        [date(2026, 8, 10), date(2026, 8, 17)],
        skipped=set(),
        cutoff_days_before=2,
        cutoff_time=time(18, 0),
        paused=True,
        now=datetime(2026, 8, 3, 9, 0),
    )
    assert {week.status for week in built} == {"paused"}
    assert not any(week.is_active for week in built)


# --- API ---------------------------------------------------------------------

def test_defaults_are_created_on_first_read(schedule_client):
    client, _ = schedule_client
    body = client.get("/api/schedule").json()

    assert body["settings"] == {
        "cadence_weeks": 1,
        "anchor_week_start": sched.format_date(_next_monday()),
        "cutoff_days_before": 2,
        "cutoff_time": "18:00",
        "paused": False,
        "horizon_weeks": 6,
        "recipes_per_week": 5,
        "default_portions": 4,
        "pack_shortfall_tolerance_pct": 10.0,
    }
    assert len(body["weeks"]) == 6
    assert body["weeks"][0]["week_start"] == sched.format_date(_next_monday())
    # Consecutive weeks at the default weekly cadence.
    starts = [sched.parse_date(week["week_start"]) for week in body["weeks"]]
    assert all((b - a).days == 7 for a, b in zip(starts, starts[1:]))


def test_settings_update_reshapes_the_window(schedule_client):
    client, _ = schedule_client
    anchor = sched.format_date(_next_monday())
    body = client.put(
        "/api/schedule/settings",
        json={"cadence_weeks": 2, "horizon_weeks": 4, "anchor_week_start": anchor},
    ).json()

    starts = [sched.parse_date(week["week_start"]) for week in body["weeks"]]
    assert len(starts) == 4
    assert all((b - a).days == 14 for a, b in zip(starts, starts[1:]))
    assert client.get("/api/schedule/settings").json()["cadence_weeks"] == 2


def test_settings_update_is_partial(schedule_client):
    client, _ = schedule_client
    client.put(
        "/api/schedule/settings",
        json={"cadence_weeks": 3, "pack_shortfall_tolerance_pct": 7.5},
    )
    settings = client.put(
        "/api/schedule/settings", json={"recipes_per_week": 7}
    ).json()["settings"]

    assert settings["cadence_weeks"] == 3
    assert settings["recipes_per_week"] == 7
    assert settings["pack_shortfall_tolerance_pct"] == 7.5


@pytest.mark.parametrize(
    "payload",
    [
        {"cadence_weeks": 0},
        {"cadence_weeks": 99},
        {"horizon_weeks": 0},
        {"recipes_per_week": 0},
        {"default_portions": 12},
        {"pack_shortfall_tolerance_pct": -0.1},
        {"pack_shortfall_tolerance_pct": 25.1},
    ],
)
def test_settings_reject_values_outside_their_range(schedule_client, payload):
    client, _ = schedule_client
    assert client.put("/api/schedule/settings", json=payload).status_code == 422


def test_runtime_schema_adds_pack_shortfall_tolerance_to_an_existing_database(tmp_path):
    engine = make_engine(tmp_path / "old-schedule.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plan_settings (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO plan_settings (id) VALUES (1)"))

    init_db(engine)

    with engine.connect() as connection:
        columns = {
            row[1]: row for row in connection.execute(text("PRAGMA table_info(plan_settings)"))
        }
        value = connection.execute(
            text("SELECT pack_shortfall_tolerance_pct FROM plan_settings")
        ).scalar()
    assert "pack_shortfall_tolerance_pct" in columns
    assert columns["pack_shortfall_tolerance_pct"][4] == "10"
    assert value == 10


def test_settings_reject_a_malformed_cutoff_time(schedule_client):
    client, _ = schedule_client
    response = client.put("/api/schedule/settings", json={"cutoff_time": "6pm"})
    assert response.status_code == 400
    assert "6pm" in response.json()["detail"]


def test_settings_reject_an_anchor_that_is_not_a_week_start(schedule_client):
    client, _ = schedule_client
    tuesday = sched.format_date(_next_monday() + timedelta(days=1))
    response = client.put(
        "/api/schedule/settings", json={"anchor_week_start": tuesday}
    )
    assert response.status_code == 400
    assert "Monday" in response.json()["detail"]


def _open_weeks(body):
    """The weeks still up for planning — the first week can already be past its
    cutoff, since the schedule shows the current week until it is over."""
    return [week for week in body["weeks"] if week["status"] == "open"]


def test_skipping_a_week_moves_the_active_week_on(schedule_client):
    client, _ = schedule_client
    before = client.get("/api/schedule").json()
    first, second = _open_weeks(before)[:2]
    assert first["is_active"]

    after = client.put(
        f"/api/schedule/weeks/{first['week_start']}", json={"skipped": True}
    ).json()

    skipped = next(w for w in after["weeks"] if w["week_start"] == first["week_start"])
    assert skipped["status"] == "skipped"
    assert after["active_week_start"] == second["week_start"]

    restored = client.put(
        f"/api/schedule/weeks/{first['week_start']}", json={"skipped": False}
    ).json()
    assert restored["active_week_start"] == first["week_start"]


def test_pausing_clears_the_active_week(schedule_client):
    client, _ = schedule_client
    body = client.put("/api/schedule/settings", json={"paused": True}).json()

    assert body["active_week_start"] is None
    assert {week["status"] for week in body["weeks"]} == {"paused"}

    resumed = client.put("/api/schedule/settings", json={"paused": False}).json()
    assert resumed["active_week_start"] == _open_weeks(resumed)[0]["week_start"]


def test_a_week_cannot_be_skipped_in_the_past(schedule_client):
    client, _ = schedule_client
    last_week = sched.format_date(_next_monday() - timedelta(days=7))
    response = client.put(f"/api/schedule/weeks/{last_week}", json={"skipped": True})
    assert response.status_code == 400


def test_only_week_starts_can_be_skipped(schedule_client):
    client, _ = schedule_client
    tuesday = sched.format_date(_next_monday() + timedelta(days=1))
    assert client.put(f"/api/schedule/weeks/{tuesday}", json={"skipped": True}).status_code == 400
    assert client.put("/api/schedule/weeks/not-a-date", json={"skipped": True}).status_code == 400


def test_week_overrides_older_than_the_history_window_are_pruned(schedule_client):
    client, factory = schedule_client
    stale = sched.format_date(_next_monday() - timedelta(weeks=sched.MAX_PAST_WEEKS + 4))
    recent = sched.format_date(_next_monday() - timedelta(weeks=2))
    with factory() as session:
        uid = user_id(session)
        session.add(PlanWeek(user_id=uid, week_start=stale, skipped=True))
        session.add(PlanWeek(user_id=uid, week_start=recent, skipped=True))
        session.commit()

    client.get("/api/schedule")

    with factory() as session:
        # The recent one is still displayable history, so it survives — a week
        # that was skipped has to keep saying so once history can show it.
        assert [row.week_start for row in session.query(PlanWeek).all()] == [recent]


def test_history_is_only_returned_when_asked_for(schedule_client):
    client, _ = schedule_client
    default = client.get("/api/schedule").json()
    assert default["past_weeks"] == []
    assert default["has_more_past"]

    body = client.get("/api/schedule", params={"past_weeks": 5}).json()
    starts = [sched.parse_date(week["week_start"]) for week in body["past_weeks"]]
    assert len(starts) == 5
    # Oldest first, and all of them behind the planning window.
    assert starts == sorted(starts)
    assert starts[-1] < sched.parse_date(body["weeks"][0]["week_start"])


def test_history_is_closed_complete_and_never_active(schedule_client):
    client, _ = schedule_client
    body = client.get("/api/schedule", params={"past_weeks": 3}).json()

    assert all(week["closed"] for week in body["past_weeks"])
    assert not any(week["is_active"] for week in body["past_weeks"])
    # The most recent past week can still be in progress (from Tuesday onwards);
    # everything older than it has finished.
    assert all(week["complete"] for week in body["past_weeks"][:-1])


def test_history_keeps_a_skipped_week_marked(schedule_client):
    client, factory = schedule_client
    skipped = sched.format_date(_next_monday() - timedelta(weeks=1))
    with factory() as session:
        session.add(PlanWeek(user_id=user_id(session), week_start=skipped, skipped=True))
        session.commit()

    body = client.get("/api/schedule", params={"past_weeks": 3}).json()
    by_start = {week["week_start"]: week for week in body["past_weeks"]}
    assert by_start[skipped]["status"] == "skipped"


def test_pausing_does_not_reach_backwards_into_history(schedule_client):
    client, _ = schedule_client
    client.put("/api/schedule/settings", json={"paused": True})

    body = client.get("/api/schedule", params={"past_weeks": 2}).json()
    assert {week["status"] for week in body["weeks"]} == {"paused"}
    assert not any(week["status"] == "paused" for week in body["past_weeks"])


def test_history_is_capped(schedule_client):
    client, _ = schedule_client
    body = client.get("/api/schedule", params={"past_weeks": sched.MAX_PAST_WEEKS}).json()
    assert len(body["past_weeks"]) == sched.MAX_PAST_WEEKS
    assert not body["has_more_past"]

    over = client.get("/api/schedule", params={"past_weeks": sched.MAX_PAST_WEEKS + 1})
    assert over.status_code == 422
