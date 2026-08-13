"""The heartbeat's scheduling, the rung-2 cap, and the event trail.

Nothing here touches a browser or Ocado. The ladder is driven through fakes so
the parts worth testing — what gets recorded, what gets skipped, and when a check
is allowed to happen — are testable without a login.
"""
from __future__ import annotations

import random
from datetime import datetime, time as dtime, timedelta

import pytest

from app.ocado.auth import SILENT_MIN_INTERVAL_S, AuthLadder, AuthState, LadderEvent
from app.ocado.heartbeat import STARTUP_DELAY, Heartbeat


class _FakeSession:
    """Just enough OcadoSession for the ladder's two probe points."""

    def __init__(self, authenticated: bool = False):
        self.authenticated = authenticated
        self.probes = 0

    def probe_authenticated(self) -> bool:
        self.probes += 1
        return self.authenticated


#: An explicit ``None`` now means "this ladder has no credentials" rather than
#: "read them from config" — see :data:`app.ocado.auth.FROM_CONFIG`. Before that
#: sentinel existed, this dict was the difference between a unit test and a real
#: browser pointed at the real Ocado login.
NO_CREDENTIALS = {"email": None, "password": None}


def _ladder(**kwargs) -> tuple[AuthLadder, list[LadderEvent]]:
    events: list[LadderEvent] = []
    ladder = AuthLadder(
        account_id="test",
        on_event=events.append,
        otp_mailbox=None,
        **NO_CREDENTIALS,
        **kwargs,
    )
    return ladder, events


# -- the event trail -------------------------------------------------------


def test_live_jar_records_a_probe_and_stops_there():
    ladder, events = _ladder()
    session = _FakeSession(authenticated=True)

    state = ladder.ensure_authenticated(session, trigger="heartbeat")

    assert state == AuthState.READY
    assert [(e.rung, e.outcome, e.trigger) for e in events] == [
        ("probe", "ok", "heartbeat")
    ]


def test_dead_jar_records_the_probe_failure_then_the_silent_attempt(monkeypatch):
    ladder, events = _ladder()
    session = _FakeSession(authenticated=False)
    monkeypatch.setattr(AuthLadder, "try_silent", lambda self, session, **kw: False)

    # allow_login=False is what the heartbeat passes: it must stop here.
    state = ladder.ensure_authenticated(session, allow_login=False, trigger="heartbeat")

    assert state == AuthState.LOGGED_OUT
    assert [(e.rung, e.outcome) for e in events] == [("probe", "failed")]


def test_a_login_without_credentials_is_recorded_as_skipped():
    ladder, events = _ladder()
    session = _FakeSession(authenticated=False)

    state = ladder.start_login(session, trigger="heartbeat")

    assert state == AuthState.LOGGED_OUT
    assert [(e.rung, e.outcome, e.detail) for e in events] == [
        ("login", "skipped", "no stored credentials")
    ]


def test_a_broken_sink_cannot_fail_a_login():
    def explode(_event):
        raise RuntimeError("sink is down")

    ladder, _ = _ladder()
    ladder.on_event = explode
    session = _FakeSession(authenticated=True)

    assert ladder.ensure_authenticated(session) == AuthState.READY


def test_the_password_is_not_in_the_repr():
    """A ladder formatted into a traceback must not print a credential."""
    ladder = AuthLadder(
        account_id="test", otp_mailbox=None, email="a@b.c", password="hunter2"
    )
    assert "hunter2" not in repr(ladder)


# -- the credential sentinel -----------------------------------------------


def test_an_explicit_none_means_no_credentials_not_the_configured_ones(monkeypatch):
    """The difference between a unit test and a real login against a real account."""
    monkeypatch.setattr("app.config.OCADO_EMAIL", "real@example.com")
    monkeypatch.setattr("app.config.OCADO_PASSWORD", "real-password")

    ladder = AuthLadder(account_id="test", otp_mailbox=None, email=None, password=None)

    assert ladder.email is None
    assert ladder.password is None


def test_an_account_without_a_password_does_not_borrow_the_default_accounts(monkeypatch):
    """Otherwise a second account silently logs in as the first one."""
    monkeypatch.setattr("app.config.OCADO_EMAIL", "nikhil@example.com")
    monkeypatch.setattr("app.config.OCADO_PASSWORD", "nikhils-password")

    # What get_account_runtime passes for an account configured without one.
    ladder = AuthLadder(
        account_id="anuja", otp_mailbox=None, email="anuja@example.com", password=None
    )

    assert ladder.password is None


def test_omitting_the_credentials_still_reads_config(monkeypatch):
    """The existing callers must keep working — that is what the sentinel is for."""
    monkeypatch.setattr("app.config.OCADO_EMAIL", "real@example.com")
    monkeypatch.setattr("app.config.OCADO_PASSWORD", "real-password")

    ladder = AuthLadder(account_id="test", otp_mailbox=None)

    assert ladder.email == "real@example.com"
    assert ladder.password == "real-password"


# -- the rung-2 cap --------------------------------------------------------


def test_silent_refresh_is_rate_limited(monkeypatch):
    """A dead session plus a retrying caller must not become a browser loop."""
    launches = []

    class _Worker:
        def submit(self, fn, timeout=None):
            launches.append(1)

    ladder, events = _ladder()
    ladder._worker = _Worker()
    session = _FakeSession(authenticated=False)

    assert ladder.try_silent(session) is False
    assert ladder.try_silent(session) is False

    assert len(launches) == 1, "the second attempt should never reach the browser"
    assert [(e.rung, e.outcome) for e in events] == [
        ("silent", "failed"),
        ("silent", "skipped"),
    ]


def test_silent_refresh_runs_again_once_the_interval_has_passed(monkeypatch):
    launches = []

    class _Worker:
        def submit(self, fn, timeout=None):
            launches.append(1)

    ladder, _ = _ladder()
    ladder._worker = _Worker()
    session = _FakeSession(authenticated=False)

    ladder.try_silent(session)
    ladder._last_silent_at -= SILENT_MIN_INTERVAL_S + 1
    ladder.try_silent(session)

    assert len(launches) == 2


# -- scheduling ------------------------------------------------------------


@pytest.fixture
def beat() -> Heartbeat:
    # A fixed seed keeps the jitter deterministic without removing it.
    return Heartbeat(interval_hours=24, window="09:00-21:00", rng=random.Random(0))


def test_a_due_time_outside_the_window_is_pushed_into_the_next_one(beat):
    middle_of_the_night = datetime(2026, 8, 14, 3, 0)

    due = beat._schedule(middle_of_the_night)

    assert beat._in_window(due), f"{due} is outside {beat.window_start}-{beat.window_end}"


def test_scheduling_lands_inside_the_window_from_any_starting_hour(beat):
    for hour in range(24):
        due = beat._schedule(datetime(2026, 8, 14, hour, 0))
        assert beat._in_window(due), f"start hour {hour} produced {due}"


def test_jitter_means_two_checks_never_line_up(beat):
    base = datetime(2026, 8, 14, 12, 0)
    times = {beat._schedule(base) for _ in range(20)}
    assert len(times) > 1


def test_a_window_that_wraps_midnight_is_understood():
    night = Heartbeat(interval_hours=24, window="21:00-06:00", rng=random.Random(0))
    assert night._in_window(datetime(2026, 8, 14, 23, 0))
    assert night._in_window(datetime(2026, 8, 14, 2, 0))
    assert not night._in_window(datetime(2026, 8, 14, 12, 0))


def test_an_unparseable_window_falls_back_rather_than_raising():
    beat = Heartbeat(interval_hours=24, window="not a window")
    assert beat.window_start == dtime(9, 0)
    assert beat.window_end == dtime(21, 0)


def test_no_account_is_checked_immediately_on_startup(monkeypatch, beat):
    """Starting the server must not itself be a reason to talk to Ocado.

    The first schedule is relative to now, so negative jitter used to put it in
    the past — and under UVICORN_RELOAD every saved file restarts the worker,
    which turned editing the app into a burst of probes.
    """
    class _Runtime:
        def __init__(self, id):
            self.account = type("A", (), {"id": id})()

    monkeypatch.setattr(
        "app.ocado.heartbeat.list_account_runtimes",
        lambda: [_Runtime("a"), _Runtime("b")],
    )

    now = datetime(2026, 8, 14, 12, 0)
    # Many draws, because the old bug only showed on negative jitter.
    for _ in range(200):
        for slot in beat._plan(now):
            assert slot.due_at >= now + STARTUP_DELAY


def test_accounts_are_staggered_across_the_interval(monkeypatch, beat):
    class _Account:
        def __init__(self, id):
            self.id = id

    class _Runtime:
        def __init__(self, id):
            self.account = _Account(id)

    monkeypatch.setattr(
        "app.ocado.heartbeat.list_account_runtimes",
        lambda: [_Runtime("a"), _Runtime("b"), _Runtime("c")],
    )

    slots = beat._plan(datetime(2026, 8, 14, 9, 0))

    assert [slot.account_id for slot in slots] == ["a", "b", "c"]
    spread = max(s.due_at for s in slots) - min(s.due_at for s in slots)
    assert spread > timedelta(hours=4), "three accounts should not check together"


def test_the_heartbeat_never_escalates_to_a_full_login(monkeypatch):
    """The one guarantee that matters: a timer must not email somebody a code."""
    calls = {}

    class _Auth:
        def ensure_authenticated(self, session, **kwargs):
            calls.update(kwargs)
            return AuthState.LOGGED_OUT

    class _Runtime:
        auth = _Auth()
        session = object()

    monkeypatch.setattr(
        "app.ocado.session.get_account_runtime", lambda account_id: _Runtime()
    )

    Heartbeat(interval_hours=24).check_account("test")

    assert calls["allow_login"] is False
    assert calls["trigger"] == "heartbeat"


def test_a_failing_check_is_swallowed(monkeypatch):
    class _Auth:
        def ensure_authenticated(self, session, **kwargs):
            raise RuntimeError("ocado is down")

    class _Runtime:
        auth = _Auth()
        session = object()

    monkeypatch.setattr(
        "app.ocado.session.get_account_runtime", lambda account_id: _Runtime()
    )

    # A failed check is a data point, not an outage.
    Heartbeat(interval_hours=24).check_account("test")
