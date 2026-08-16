"""The ladder that keeps a Sainsbury's session live.

The rung ordering is the point: probing is cheap, refreshing is silent, and only
the third rung can cost somebody an email. What is pinned hardest here is that
an interactive caller never waits on the mailbox — that wait is invisible while
it happens, so getting it wrong reads as a hung program rather than a slow one.
"""
from __future__ import annotations

import pytest

from app.sainsburys import session as session_module
from datetime import datetime, timedelta, timezone

from app.sainsburys.auth import (
    AuthError,
    Authorization,
    AuthState,
    PendingLogin,
    Tokens,
)
from app.sainsburys.session import SainsburysSession, _cookie_from_json

PARKED = Authorization(
    verifier="v",
    pending=PendingLogin(login_challenge="c", code_verifier="v", state="s"),
)


class FakeHttp:
    def __init__(self):
        self.cookies = _Cookies()

    def get(self, *args, **kwargs):
        return _Response(401)

    def post(self, *args, **kwargs):
        return _Response(200)

    def request(self, *args, **kwargs):
        return _Response(200)

    def close(self):
        pass


class _Cookies:
    def __init__(self):
        self.jar = _Jar()


class _Jar:
    def __init__(self):
        self._cookies = []

    def __iter__(self):
        return iter(self._cookies)

    def set_cookie(self, cookie):
        self._cookies.append(cookie)

    def clear(self):
        self._cookies = []


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code
        self.content = b""
        # No ``location``, so a redirect chase ends here rather than walking off
        # into the real provider. The unpatched authorize rung reaches this.
        self.headers = {}

    def json(self):
        return {}


@pytest.fixture
def session(tmp_path, monkeypatch):
    # Nothing below should reach the network; anything that tries fails loudly.
    # The provider does not remember this client, so every login reaches the
    # password step. The rung that avoids that has its own tests below.
    monkeypatch.setattr(session_module, "authorize", lambda http: PARKED)
    monkeypatch.setattr(session_module, "submit_credentials", lambda *a, **k: None)
    return SainsburysSession(
        jar_path=tmp_path / "session.json",
        http=FakeHttp(),
    )


def test_an_interactive_login_never_waits_on_the_mailbox(session, monkeypatch):
    """The regression that made `python -m app.sainsburys login` look hung.

    Reading the mailbox blocks for the whole OTP wait with nothing to print, so
    an interactive login that opts into it is two silent minutes before the
    prompt appears.
    """
    called = []
    monkeypatch.setattr(
        session_module, "read_emailed_code", lambda **kwargs: called.append(kwargs) or None
    )

    state = session.ensure_authenticated(
        trust_existing=False,
        email="a@b.com",
        password="pw",
        allow_mailbox=False,
    )

    assert state == AuthState.AWAITING_OTP
    assert called == [], "an interactive login must not poll the mailbox"


def test_an_unattended_login_still_reads_the_mailbox(session, monkeypatch):
    """The API path, where there is nobody to ask."""
    called = []
    monkeypatch.setattr(
        session_module, "read_emailed_code", lambda **kwargs: called.append(kwargs) or None
    )

    session.ensure_authenticated(
        trust_existing=False, email="a@b.com", password="pw"
    )

    assert len(called) == 1


def test_a_parked_login_keeps_what_the_code_will_need(session, monkeypatch):
    monkeypatch.setattr(session_module, "read_emailed_code", lambda **kwargs: None)

    session.ensure_authenticated(
        trust_existing=False,
        email="a@b.com",
        password="pw",
        allow_mailbox=False,
    )

    # The PKCE verifier has to survive to the OTP submit, which is a separate
    # request minutes later; losing it means another emailed code.
    assert session.pending is not None
    assert session.pending.code_verifier == "v"


def test_a_quiet_refresh_never_reaches_the_password(session, monkeypatch):
    """Safe to call on page load: it cannot email anybody a code."""
    attempted = []
    monkeypatch.setattr(
        session_module,
        "submit_credentials",
        lambda *a, **k: attempted.append(True),
    )

    assert session.refresh_quietly() == AuthState.NEEDS_PASSWORD
    assert attempted == []


# --- the rung that decides whether anybody is emailed a code ----------------
#
# Signing in through a browser never asks for a one-time code, while this ladder
# always did. The reason was not that Sainsbury's trusts the browser's device: it
# is that the browser never posts a password, because the provider answers its
# authorization request with a code outright. Posting a password is what sends
# the email, so a ladder that always reached the password step always paid for
# one. These pin the rung that avoids it.


def _provider_cookie(session):
    """Give the jar an identity cookie, so rung 4 is worth attempting.

    Without one the rung is skipped on principle: a provider session *is* a
    cookie, so an empty jar cannot have one and the round trip would be spent
    learning what the jar already said.
    """
    # A real Cookie rather than a stand-in: these get written out by save(),
    # which reads fields a stub does not have.
    session.http.cookies.jar.set_cookie(
        _cookie_from_json(
            {
                "name": "oauth2_authentication_session",
                "value": "provider-session",
                "domain": "account.sainsburys.co.uk",
            }
        )
    )


def _settled(session, monkeypatch, seen):
    """Redeeming a code works, without a network."""
    monkeypatch.setattr(
        session_module,
        "exchange_code",
        lambda http, code, verifier: seen.append((code, verifier))
        or Tokens(access_token="fresh", refresh_token="r"),
    )
    monkeypatch.setattr(session_module, "establish_gol_session", lambda http, tokens: None)


def test_a_remembered_provider_session_signs_in_without_a_password(session, monkeypatch):
    """The whole point: no credential sent, so no code can be emailed."""
    seen: list[tuple[str, str]] = []
    _provider_cookie(session)
    _settled(session, monkeypatch, seen)
    monkeypatch.setattr(
        session_module, "authorize", lambda http: Authorization(verifier="v2", code="auth-code")
    )
    attempted = []
    monkeypatch.setattr(
        session_module, "submit_credentials", lambda *a, **k: attempted.append(True)
    )

    state = session.ensure_authenticated(
        trust_existing=False, email="a@b.com", password="pw"
    )

    assert state == AuthState.READY
    assert seen == [("auth-code", "v2")], "the offered code should have been redeemed"
    assert attempted == [], "a password must not be posted when none was asked for"


def test_the_quiet_path_can_use_it_too(session, monkeypatch):
    """It sends no credential, so a page load may take this rung safely."""
    _provider_cookie(session)
    _settled(session, monkeypatch, [])
    monkeypatch.setattr(
        session_module, "authorize", lambda http: Authorization(verifier="v", code="c")
    )

    assert session.refresh_quietly() == AuthState.READY


def test_a_provider_that_wants_a_password_still_falls_through_to_one(session, monkeypatch):
    """The rung is an optimisation, not a replacement."""
    posted = []
    monkeypatch.setattr(
        session_module, "submit_credentials", lambda *a, **k: posted.append(True)
    )

    session.ensure_authenticated(trust_existing=False, email="a@b.com", password="pw")

    assert posted == [True]


def test_a_provider_session_that_will_not_redeem_is_not_fatal(session, monkeypatch):
    """An offered code the token endpoint then refuses. Fall back, do not crash."""
    _provider_cookie(session)
    monkeypatch.setattr(
        session_module, "authorize", lambda http: Authorization(verifier="v", code="c")
    )

    def refuse(*args, **kwargs):
        raise AuthError("no")

    monkeypatch.setattr(session_module, "exchange_code", refuse)

    assert session.refresh_quietly() == AuthState.NEEDS_PASSWORD


def test_the_csrf_pair_alone_is_not_a_session(session, monkeypatch):
    """What a jar holds after a *successful* login, measured against the live site.

    The provider issues these two on the way to the login form, to anyone. A
    jar that has genuinely signed in holds exactly these and nothing else, so
    reading them as "we have a session" would make rung four fire on every page
    load and always fall through to the password step it exists to avoid.
    """
    asked = []
    monkeypatch.setattr(
        session_module, "authorize", lambda http: asked.append(True) or PARKED
    )
    for name in ("oauth2_authentication_csrf", "oauth2_consent_csrf"):
        session.http.cookies.jar.set_cookie(
            _cookie_from_json(
                {"name": name, "value": "x", "domain": "account.sainsburys.co.uk"}
            )
        )

    assert session.has_provider_session() is False
    assert session.refresh_quietly() == AuthState.NEEDS_PASSWORD
    assert asked == []


def test_an_empty_jar_does_not_pay_for_a_round_trip_to_learn_nothing(
    session, monkeypatch
):
    """The page-load path for an account nobody has connected.

    A provider session is a cookie. With no identity cookie in the jar there can
    be no session to find, and asking cost a second of network on every page
    load before this guard existed.
    """
    asked = []
    monkeypatch.setattr(
        session_module, "authorize", lambda http: asked.append(True) or PARKED
    )

    assert session.refresh_quietly() == AuthState.NEEDS_PASSWORD
    assert asked == [], "nothing to ask about, so nothing should have been asked"


def _expired() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=1)


def test_a_live_access_token_re_mints_the_session_without_spending_the_refresh(
    session, monkeypatch
):
    """The cookies die well inside the token's hour, so this is the usual repair.

    Going straight to the refresh token would work too, and would cost one: they
    rotate on use, so every needless refresh is a new credential to persist and
    an old one that stops working.
    """
    session.tokens = Tokens(access_token="live", refresh_token="rt")
    monkeypatch.setattr(session_module, "establish_gol_session", lambda http, tokens: None)
    monkeypatch.setattr(
        session_module,
        "refresh_tokens",
        lambda *a, **k: pytest.fail("a live access token must not spend the refresh token"),
    )

    assert session.refresh_quietly() == AuthState.READY
    assert session.tokens.refresh_token == "rt"


def test_an_expired_access_token_falls_through_to_the_refresh_token(session, monkeypatch):
    session.tokens = Tokens(access_token="old", refresh_token="rt", expires_at=_expired())
    monkeypatch.setattr(
        session_module,
        "refresh_tokens",
        lambda http, token: Tokens(access_token="new", refresh_token="rt2"),
    )
    monkeypatch.setattr(session_module, "establish_gol_session", lambda http, tokens: None)
    monkeypatch.setattr(
        session_module,
        "submit_credentials",
        lambda *a, **k: pytest.fail("a working refresh token must not trigger a login"),
    )

    assert session.refresh_quietly() == AuthState.READY
    assert session.tokens.access_token == "new"


def test_a_rotated_refresh_token_is_persisted_before_anything_else_can_fail(
    session, monkeypatch
):
    """The provider invalidates the old one the moment it issues the new one.

    So if the rotated token is only written after the session call succeeds, a
    failure in between leaves the stored session holding a spent credential and
    no way back but a one-time code.
    """
    session.tokens = Tokens(access_token="old", refresh_token="rt", expires_at=_expired())
    monkeypatch.setattr(
        session_module,
        "refresh_tokens",
        lambda http, token: Tokens(access_token="new", refresh_token="rotated"),
    )
    monkeypatch.setattr(
        session_module,
        "establish_gol_session",
        lambda http, tokens: (_ for _ in ()).throw(AuthError("nope")),
    )

    assert session.refresh_quietly() == AuthState.NEEDS_PASSWORD

    reloaded = SainsburysSession(jar_path=session.jar_path, http=FakeHttp())
    assert reloaded.tokens.refresh_token == "rotated"


def test_a_dead_refresh_token_does_not_take_the_access_token_with_it(session, monkeypatch):
    """They are separate credentials with separate expiries.

    Discarding both is how a session that only needed re-minting turns into a
    login prompt — and, with it, a one-time code somebody has to go and read.
    """
    session.tokens = Tokens(access_token="still-good", refresh_token="revoked")
    monkeypatch.setattr(
        session_module,
        "refresh_tokens",
        lambda *a, **k: (_ for _ in ()).throw(AuthError("revoked")),
    )

    session._refresh()

    assert session.tokens is not None
    assert session.tokens.access_token == "still-good"
    assert session.tokens.refresh_token is None


def test_credentials_are_not_retained_on_the_session(tmp_path):
    bare = SainsburysSession(jar_path=tmp_path / "session.json", http=FakeHttp())

    assert bare.ensure_authenticated(trust_existing=False) == AuthState.NEEDS_PASSWORD
    assert not hasattr(bare, "email")
    assert not hasattr(bare, "password")


def test_the_login_asks_for_the_code_to_be_sent(session, monkeypatch):
    """Otherwise the ladder parks waiting for a code that was never dispatched."""
    asked = []
    monkeypatch.setattr(session_module, "request_code", lambda http: asked.append(True))
    monkeypatch.setattr(session_module, "read_emailed_code", lambda **kwargs: None)

    state = session.ensure_authenticated(
        trust_existing=False,
        email="a@b.com",
        password="pw",
        allow_mailbox=False,
    )

    assert asked == [True]
    assert state == AuthState.AWAITING_OTP


def test_every_request_carries_the_commerce_token_and_a_bearer(session, monkeypatch):
    """Reads are answered without these; writes are refused 401.

    That asymmetry is the trap: an integration missing them looks completely
    healthy — the basket reads back fine — right up until it tries to put
    something in the trolley.
    """
    sent = {}

    class Recording(FakeHttp):
        def request(self, method, url, headers=None, **kwargs):
            sent.update(headers or {})
            return _Response(200)

    session._http = Recording()
    session._http.cookies.jar.set_cookie(
        _cookie("WC_AUTHENTICATION_724909769", "wc-value")
    )
    session.tokens = Tokens(access_token="at")

    session.request("POST", "/groceries-api/gol-services/basket/v2/basket/items")

    assert sent["WCAuthToken"] == "wc-value"
    assert sent["Authorization"] == "Bearer at"


def test_a_401_never_escalates_to_the_password_step(session, monkeypatch):
    calls = []

    class Rejecting(FakeHttp):
        def request(self, *args, **kwargs):
            calls.append(1)
            return _Response(401)

    session._http = Rejecting()
    monkeypatch.setattr(
        session_module,
        "submit_credentials",
        lambda *args, **kwargs: pytest.fail("a 401 retry must not submit a password"),
    )

    response = session.request("GET", "/groceries-api/gol-services/basket/v2/basket")

    assert response.status_code == 401
    assert calls == [1]
    assert session.state == AuthState.NEEDS_PASSWORD


def test_a_tombstoned_commerce_cookie_is_not_a_session(session):
    """WebSphere marks a logged-out session by setting the value to DEL."""
    session._http.cookies.jar.set_cookie(_cookie("WC_AUTHENTICATION_1", "DEL"))

    assert session.wc_auth_token() is None
    assert session.has_auth_cookie() is False


def test_the_store_specific_cookie_is_only_a_fallback(session):
    session._http.cookies.jar.set_cookie(_cookie("WC_AUTHENTICATION_9-1002", "other-store"))
    assert session.wc_auth_token() == "other-store"

    session._http.cookies.jar.set_cookie(_cookie("WC_AUTHENTICATION_9", "primary"))
    assert session.wc_auth_token() == "primary"


def _cookie(name: str, value: str):
    from http.cookiejar import Cookie

    return Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain="www.sainsburys.co.uk", domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True, secure=True, expires=None, discard=False,
        comment=None, comment_url=None, rest={}, rfc2109=False,
    )
