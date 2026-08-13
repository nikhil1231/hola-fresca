"""The auth-event trail: what gets written, and what the summary makes of it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import main
from app.api.deps import get_session
from app.db.models import OcadoAuthEvent, User
from app.ocado import events as events_mod
from app.ocado.auth import LadderEvent


@pytest.fixture
def client(factory):
    main.app.dependency_overrides[get_session] = lambda: factory()
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


def _event(**kwargs) -> LadderEvent:
    return LadderEvent(
        account_id=kwargs.pop("account_id", "nikhil"),
        rung=kwargs.pop("rung", "probe"),
        outcome=kwargs.pop("outcome", "ok"),
        trigger=kwargs.pop("trigger", "heartbeat"),
        **kwargs,
    )


def _seed(factory, account_id, rung, outcome, when, trigger="heartbeat"):
    with factory() as session:
        session.add(
            OcadoAuthEvent(
                account_id=account_id,
                rung=rung,
                outcome=outcome,
                trigger=trigger,
                created_at=when,
            )
        )
        session.commit()


# -- the sink --------------------------------------------------------------


def test_nothing_is_recorded_until_a_sink_is_installed():
    """The default is silence, so importing the app cannot start writing."""
    events_mod.set_sink(None)
    events_mod.record(_event())  # must not raise


def test_the_db_sink_appends_a_row(factory):
    events_mod.set_sink(events_mod.db_sink(lambda: factory))
    try:
        events_mod.record(_event(rung="silent", outcome="ok", duration_ms=1234))
    finally:
        events_mod.set_sink(None)

    with factory() as session:
        rows = list(session.scalars(select(OcadoAuthEvent)))

    assert len(rows) == 1
    assert (rows[0].account_id, rows[0].rung, rows[0].outcome) == ("nikhil", "silent", "ok")
    assert rows[0].duration_ms == 1234


def test_the_sink_swallows_a_broken_database():
    def boom():
        raise RuntimeError("no database here")

    events_mod.set_sink(events_mod.db_sink(boom))
    try:
        events_mod.record(_event())  # a login must not fail over telemetry
    finally:
        events_mod.set_sink(None)


def test_the_factory_is_not_built_until_something_is_recorded():
    """Installing the sink must not open an engine — see db_sink's docstring."""
    built = []

    def get_factory():
        built.append(1)
        raise AssertionError("should not be reached")

    events_mod.set_sink(events_mod.db_sink(get_factory))
    events_mod.set_sink(None)

    assert built == []


# -- the summary -----------------------------------------------------------


def test_summary_reports_the_silent_to_login_ratio(client, factory):
    now = datetime.now(timezone.utc)
    for days_ago in (10, 8, 6, 4):
        _seed(factory, "nikhil", "silent", "ok", now - timedelta(days=days_ago))
    _seed(factory, "nikhil", "login", "ok", now - timedelta(days=12))
    _seed(factory, "nikhil", "login", "ok", now - timedelta(days=2))

    body = client.get("/api/ocado/auth-events").json()

    assert len(body["accounts"]) == 1
    summary = body["accounts"][0]
    assert summary["account_id"] == "nikhil"
    assert summary["silent_ok"] == 4
    assert summary["logins"] == 2
    assert summary["silent_per_login"] == 2.0
    # Twelve days ago to two days ago, the gap between consecutive full logins.
    assert summary["longest_stretch_hours"] == pytest.approx(240.0, abs=1.0)


def test_a_ratio_needs_a_login_to_divide_by(client, factory):
    """No interruptions yet is not the same as an infinitely good ratio."""
    _seed(factory, "nikhil", "silent", "ok", datetime.now(timezone.utc))

    summary = client.get("/api/ocado/auth-events").json()["accounts"][0]

    assert summary["silent_per_login"] is None
    assert summary["longest_stretch_hours"] is None


def test_accounts_are_summarised_separately(client, factory):
    now = datetime.now(timezone.utc)
    _seed(factory, "nikhil", "silent", "ok", now)
    _seed(factory, "anuja", "login", "ok", now)

    body = client.get("/api/ocado/auth-events").json()

    assert [a["account_id"] for a in body["accounts"]] == ["anuja", "nikhil"]


def test_events_outside_the_window_are_excluded(client, factory):
    old = datetime.now(timezone.utc) - timedelta(days=200)
    _seed(factory, "nikhil", "silent", "ok", old)

    body = client.get("/api/ocado/auth-events?days=90").json()

    assert body["events"] == []
    assert body["accounts"] == []


def test_the_trail_is_admin_only(client, factory):
    """Whose Ocado connection is broken is not one user's business about another."""
    with factory() as session:
        session.add(User(is_admin=0, email="friend@example.com"))
        session.commit()
        # get_current_user takes the lowest id, so demote the bootstrap account
        # rather than relying on ordering to pick the non-admin one.
        bootstrap = session.scalars(select(User).order_by(User.id)).first()
        bootstrap.is_admin = 0
        session.commit()

    assert client.get("/api/ocado/auth-events").status_code == 403
