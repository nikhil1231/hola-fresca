"""Shopping schedule: cadence arithmetic, cutoffs, skips and pausing."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from app import schedule as sched
from app.api.deps import get_session
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
    client.put("/api/schedule/settings", json={"cadence_weeks": 3})
    settings = client.put(
        "/api/schedule/settings", json={"recipes_per_week": 7}
    ).json()["settings"]

    assert settings["cadence_weeks"] == 3
    assert settings["recipes_per_week"] == 7


@pytest.mark.parametrize(
    "payload",
    [
        {"cadence_weeks": 0},
        {"cadence_weeks": 99},
        {"horizon_weeks": 0},
        {"recipes_per_week": 0},
        {"default_portions": 12},
    ],
)
def test_settings_reject_values_outside_their_range(schedule_client, payload):
    client, _ = schedule_client
    assert client.put("/api/schedule/settings", json=payload).status_code == 422


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


def test_past_week_overrides_are_pruned_on_read(schedule_client):
    client, factory = schedule_client
    stale = sched.format_date(_next_monday() - timedelta(days=21))
    with factory() as session:
        session.add(PlanWeek(week_start=stale, skipped=True))
        session.commit()

    client.get("/api/schedule")

    with factory() as session:
        assert session.query(PlanWeek).count() == 0
