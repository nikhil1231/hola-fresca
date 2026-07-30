"""Auth ladder escalation. The browser is faked - these never launch Playwright."""
from __future__ import annotations

import pytest

from app.ocado.auth import AuthLadder, AuthState, _await_login_outcome


class FakeSession:
    """Just enough OcadoSession for the ladder: a probe with a scripted answer."""

    def __init__(self, *, authenticated=False):
        self.authenticated = authenticated
        self.probes = 0
        self.imported = []

    def probe_authenticated(self):
        self.probes += 1
        return self.authenticated

    def import_playwright_cookies(self, cookies):
        self.imported.append(cookies)


def ladder(*, silent_works=False, login_outcome=None, session_after=None):
    """A ladder with both browser steps stubbed out."""
    made = AuthLadder(profile_dir=None, headless=True)
    calls = []

    def try_silent(session):
        calls.append("silent")
        if silent_works:
            session.authenticated = True
        return silent_works

    def start_login(session):
        calls.append("login")
        if session_after is not None:
            session.authenticated = session_after
        made.state = login_outcome or AuthState.LOGGED_OUT
        return made.state

    made.try_silent = try_silent
    made.start_login = start_login
    made.calls = calls
    return made


def test_a_working_jar_short_circuits_the_ladder():
    auth = ladder()
    session = FakeSession(authenticated=True)

    assert auth.ensure_authenticated(session) == AuthState.READY
    assert auth.calls == []


def test_a_dead_jar_escalates_to_silent_refresh():
    auth = ladder(silent_works=True)
    session = FakeSession(authenticated=False)

    assert auth.ensure_authenticated(session) == AuthState.READY
    assert auth.calls == ["silent"]


def test_a_failed_silent_refresh_escalates_to_full_login():
    auth = ladder(silent_works=False, login_outcome=AuthState.AWAITING_OTP)
    session = FakeSession(authenticated=False)

    assert auth.ensure_authenticated(session) == AuthState.AWAITING_OTP
    assert auth.calls == ["silent", "login"]


def test_a_quiet_refresh_never_reaches_the_password_step():
    """What makes auto-reconnect-on-page-load safe.

    The first two rungs need nothing from the user; the third emails an OTP. So
    anything automatic must stop before it, or merely opening the page would
    send a code.
    """
    auth = ladder(silent_works=False, login_outcome=AuthState.AWAITING_OTP)
    session = FakeSession(authenticated=False)

    state = auth.ensure_authenticated(session, allow_login=False)

    assert state == AuthState.LOGGED_OUT
    assert auth.calls == ["silent"], "must not start a login that emails a code"


def test_a_quiet_refresh_still_reports_a_working_session():
    auth = ladder(silent_works=True)
    session = FakeSession(authenticated=False)

    assert auth.ensure_authenticated(session, allow_login=False) == AuthState.READY


def test_a_401_caller_is_not_allowed_to_short_circuit():
    """The regression that made re-auth impossible.

    ``global_sid`` outlives its own validity, so a stale-but-present cookie used
    to read as authenticated. Reached from a 401 the jar is provably dead, and
    trusting it means retrying the same dead cookie forever.
    """
    auth = ladder(silent_works=True)
    session = FakeSession(authenticated=True)  # would pass a probe, but is stale

    state = auth.ensure_authenticated(session, trust_existing=False)

    assert state == AuthState.READY
    assert auth.calls == ["silent"], "must refresh rather than trust the jar"
    assert session.probes == 0, "must not waste a probe it already knows the answer to"


class FakePage:
    """A page whose URL and visible OTP field are scripted per poll."""

    def __init__(self, urls, otp_visible=True):
        self.urls = list(urls)
        self.otp_visible = otp_visible
        self.polls = 0

    @property
    def url(self):
        return self.urls[min(self.polls, len(self.urls) - 1)]

    def locator(self, selector):
        page = self

        class Loc:
            first = property(lambda s: s)

            def count(self):
                return 1 if page.otp_visible else 0

            def is_visible(self):
                return page.otp_visible

            def inner_text(self):
                return ""

        return Loc()

    def wait_for_timeout(self, ms):
        self.polls += 1


def test_the_otp_step_ignores_the_prompt_it_just_submitted():
    """The regression that threw away every correct code.

    Right after the code is submitted the OTP field is still on screen, so
    watching for it matches instantly and reports the step done before Ocado has
    checked anything - after which the caller closes the browser.
    """
    page = FakePage(["https://sso.ocado.com/ocado/login"] * 3 + ["https://www.ocado.com/"])

    outcome, _ = _await_login_outcome(page, timeout=5, watch_for_otp=False)

    assert outcome == "ready", "must wait for the redirect, not the prompt it typed into"


def test_the_password_step_does_watch_for_the_prompt():
    page = FakePage(["https://sso.ocado.com/ocado/login"], otp_visible=True)

    outcome, _ = _await_login_outcome(page, timeout=5, watch_for_otp=True)

    assert outcome == "otp"


def test_a_slow_otp_times_out_without_claiming_success():
    page = FakePage(["https://sso.ocado.com/ocado/login"], otp_visible=True)

    outcome, _ = _await_login_outcome(page, timeout=0.2, watch_for_otp=False)

    assert outcome == "timeout"


def test_otp_without_a_pending_login_reports_logged_out():
    auth = AuthLadder(profile_dir=None, headless=True)
    assert auth.submit_otp("123456") == AuthState.LOGGED_OUT


def test_an_empty_otp_is_rejected():
    auth = AuthLadder(profile_dir=None, headless=True)
    auth._pending_session = FakeSession()
    try:
        auth.submit_otp("   ")
    except ValueError as exc:
        assert "required" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a ValueError")
