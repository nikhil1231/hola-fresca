"""Auth ladder escalation. The browser is faked - these never launch Playwright."""
from __future__ import annotations

import pytest

from types import SimpleNamespace

from app.ocado import auth as auth_module
from app.ocado.auth import AuthLadder, AuthStage, AuthState, _await_login_outcome
from app.ocado.otp_mail import MailboxConfig


class FakeSession:
    """Just enough OcadoSession for the ladder: a probe with a scripted answer.

    ``authenticated_after`` models the real thing: Ocado's session only starts
    working a few seconds after it accepts the login.
    """

    def __init__(self, *, authenticated=False, authenticated_after=None):
        self.authenticated = authenticated
        self.authenticated_after = authenticated_after
        self.probes = 0
        self.imported = []
        self.client = SimpleNamespace(cookies=SimpleNamespace(jar=[]))

    def probe_authenticated(self):
        self.probes += 1
        if self.authenticated_after is not None and self.probes >= self.authenticated_after:
            self.authenticated = True
        return self.authenticated

    def import_playwright_cookies(self, cookies):
        self.imported.append(cookies)


class FakeWorker:
    """Runs jobs inline. Its context and page are None, so the browser touches
    inside a harvest job are all no-ops and only the retry loop is under test."""

    def __init__(self):
        self.jobs = 0
        self.stopped = 0

    def submit(self, fn, timeout=None):
        self.jobs += 1
        return fn(self)

    def stop_browser(self):
        self.stopped += 1

    page = None
    context = None


def ladder(*, silent_works=False, login_outcome=None, session_after=None):
    """A ladder with both browser steps stubbed out."""
    made = AuthLadder(profile_dir=None, headless=True)
    calls = []

    # ``**kwargs`` swallows the ``trigger`` the ladder now threads through every
    # rung; these doubles stand in for the browser, not for the bookkeeping.
    def try_silent(session, **kwargs):
        calls.append("silent")
        if silent_works:
            session.authenticated = True
        return silent_works

    def start_login(session, **kwargs):
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


def auto_ladder(*, submit=None):
    """A ladder whose mailbox is scripted and whose browser is stubbed out."""
    auth = AuthLadder(
        profile_dir=None,
        headless=True,
        otp_mailbox=MailboxConfig(host="h", port=993, user="u@gmail.com", password="p"),
        otp_markers=("shopper@example.com",),
    )
    auth.state = AuthState.AWAITING_OTP
    auth.submitted = []

    def submit_otp(code, **kwargs):
        auth.submitted.append(code)
        # The real one records the outcome on the ladder before returning it.
        auth.state = (submit or (lambda: AuthState.READY))()
        return auth.state

    auth.submit_otp = submit_otp
    return auth


def patch_fetch(monkeypatch, result):
    calls = {}

    def fetch_code(config, query, *, since, wait_s, poll_s):
        calls["since"] = since
        calls["markers"] = query.markers
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("app.ocado.auth.otp_mail.fetch_code", fetch_code)
    return calls


def test_an_emailed_code_is_submitted_without_anyone_present(monkeypatch):
    """The whole point: a login that reaches the OTP step finishes on its own."""
    patch_fetch(monkeypatch, "481920")
    auth = auto_ladder()

    assert auth._auto_otp(since=100.0) is True
    assert auth.submitted == ["481920"]


def test_the_mailbox_is_only_asked_about_mail_newer_than_this_attempt(monkeypatch):
    calls = patch_fetch(monkeypatch, "481920")
    auth = auto_ladder()

    auth._auto_otp(since=1234.0)

    assert calls["since"] == 1234.0
    assert calls["markers"] == ("shopper@example.com",)


def test_a_silent_mailbox_leaves_the_login_waiting_for_a_human(monkeypatch):
    """Auto-OTP is an accelerator, not a replacement.

    The browser is still parked on the prompt, so anything that goes wrong here
    has to leave POST /ocado/otp able to finish the job by hand.
    """
    patch_fetch(monkeypatch, None)
    auth = auto_ladder()

    assert auth._auto_otp(since=100.0) is False
    assert auth.submitted == []
    assert auth.state == AuthState.AWAITING_OTP


def test_a_broken_mailbox_does_not_fail_the_login(monkeypatch):
    patch_fetch(monkeypatch, RuntimeError("AUTHENTICATIONFAILED"))
    auth = auto_ladder()

    assert auth._auto_otp(since=100.0) is False
    assert auth.state == AuthState.AWAITING_OTP


def test_a_rejected_code_still_leaves_the_prompt_open(monkeypatch):
    patch_fetch(monkeypatch, "000000")

    def reject():
        raise RuntimeError("Ocado rejected the code")

    auth = auto_ladder(submit=reject)

    assert auth._auto_otp(since=100.0) is False
    assert auth.state == AuthState.AWAITING_OTP


def test_no_configured_mailbox_keeps_the_old_manual_behaviour(monkeypatch):
    monkeypatch.setattr("app.ocado.auth.otp_mail.mailbox_from_env", lambda: None)
    auth = AuthLadder(profile_dir=None, headless=True)
    auth.state = AuthState.AWAITING_OTP

    assert auth._auto_otp(since=100.0) is False
    assert auth.state == AuthState.AWAITING_OTP


def test_the_stage_says_what_it_is_waiting_for_while_it_waits(monkeypatch):
    """What the sign-in button reads while the request is still blocked.

    A full login runs for minutes and its *state* is AWAITING_OTP for most of
    them, which on its own would tell you to go and read your email - the one
    thing the app is busy doing for you.
    """
    seen = []

    def fetch_code(config, query, *, since, wait_s, poll_s):
        seen.append(auth.stage)
        return "481920"

    monkeypatch.setattr("app.ocado.auth.otp_mail.fetch_code", fetch_code)
    auth = auto_ladder()

    auth._auto_otp(since=100.0)

    assert seen == [AuthStage.WAITING_FOR_CODE]


def test_the_stage_is_cleared_once_the_ladder_finishes():
    auth = ladder(silent_works=True)
    auth.ensure_authenticated(FakeSession())

    assert auth.stage == AuthStage.IDLE


def test_a_failed_login_does_not_leave_a_stage_stuck_on_screen():
    """Otherwise the button spins forever on a login that already gave up."""
    auth = ladder(silent_works=False)

    def explode(session, **kwargs):
        raise RuntimeError("browser died")

    auth.start_login = explode

    with pytest.raises(RuntimeError):
        auth.ensure_authenticated(FakeSession())
    assert auth.stage == AuthStage.IDLE


def settling_ladder(monkeypatch, *, timeout=30.0):
    monkeypatch.setattr(auth_module, "SESSION_SETTLE_POLL_S", 0)
    monkeypatch.setattr(auth_module, "SESSION_SETTLE_TIMEOUT_S", timeout)
    auth = AuthLadder(profile_dir=None, headless=True)
    auth._worker = FakeWorker()
    return auth


def test_the_jar_is_reharvested_until_the_session_actually_works(monkeypatch):
    """The regression that reported a successful login as a failed one.

    Ocado accepts the code and the URL leaves /login while its SSO is still
    redirecting, so the first harvest catches global_sid and the in-flight sso.*
    values but not the session cookie the site issues at the end of the chain.
    Harvesting once left a jar of 25 cookies that answered 401.
    """
    auth = settling_ladder(monkeypatch)
    session = FakeSession(authenticated_after=3)

    auth._finish(session)

    assert auth.state == AuthState.READY
    assert session.probes == 3
    # 3 harvests + the stop; a re-probe alone would never pick up the new cookie.
    assert auth._worker.jobs == 4


def test_a_settled_session_is_not_harvested_twice(monkeypatch):
    auth = settling_ladder(monkeypatch)
    session = FakeSession(authenticated=True)

    auth._finish(session)

    assert auth.state == AuthState.READY
    assert session.probes == 1


def test_a_session_that_never_settles_gives_up_instead_of_hanging(monkeypatch):
    auth = settling_ladder(monkeypatch, timeout=0)
    session = FakeSession(authenticated=False)

    auth._finish(session)

    assert auth.state == AuthState.LOGGED_OUT
    assert session.probes == 1
    assert auth._worker.stopped == 1, "the browser must not be left holding the profile lock"


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
