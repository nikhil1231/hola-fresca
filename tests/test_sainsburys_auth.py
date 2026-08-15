"""Signing in to Sainsbury's over HTTP.

The ladder is worth testing at the protocol level because every rung is a
redirect whose *destination* is the only thing distinguishing success from
failure - the provider answers a wrong password and a code request with the same
302, and telling them apart by where they point is the whole of the logic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.sainsburys import auth
from app.sainsburys.auth import AuthError, Tokens


class FakeResponse:
    def __init__(self, status_code=200, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHttp:
    """A scripted provider: URL substring -> response, plus a call log."""

    def __init__(self, routes: dict[str, FakeResponse]):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def _respond(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        raise AssertionError(f"no scripted response for {method} {url}")

    def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._respond("POST", url, **kwargs)


def test_begin_login_takes_the_challenge_off_the_redirect():
    http = FakeHttp(
        {
            "/oauth2/auth": FakeResponse(
                302, {"location": "https://account.sainsburys.co.uk/login-init?login_challenge=abc123"}
            ),
        }
    )

    pending = auth.begin_login(http)

    assert pending.login_challenge == "abc123"
    # PKCE has to survive to the token exchange, and the challenge sent must be
    # the hash of the verifier kept - not the verifier itself.
    assert pending.code_verifier
    sent = http.calls[0][1]
    assert "code_challenge_method=S256" in sent
    assert pending.code_verifier not in sent


def test_begin_login_follows_a_chain_of_redirects():
    # The real provider bounces through /login-init before naming the login UI.
    http = FakeHttp(
        {
            "/oauth2/auth": FakeResponse(302, {"location": "/login-init"}),
            "/login-init": FakeResponse(302, {"location": "/gol/login?login_challenge=deep"}),
        }
    )

    assert auth.begin_login(http).login_challenge == "deep"


def test_begin_login_gives_up_rather_than_looping():
    http = FakeHttp({"": FakeResponse(302, {"location": "/round-and-round"})})

    with pytest.raises(AuthError, match="redirected more than"):
        auth.begin_login(http)


def _pending() -> auth.PendingLogin:
    return auth.PendingLogin(login_challenge="chal", code_verifier="verifier", state="state")


def test_a_password_that_needs_a_code_reports_no_authorization_code():
    http = FakeHttp({"/gol/login": FakeResponse(302, {"location": "/gol/login/mfa"})})

    assert auth.submit_credentials(http, _pending(), "a@b.com", "pw") is None


def test_a_rejected_password_is_told_apart_from_a_code_request():
    # The provider re-renders the form with a 200 rather than saying so.
    http = FakeHttp({"/gol/login": FakeResponse(200)})

    with pytest.raises(AuthError, match="rejected the email or password"):
        auth.submit_credentials(http, _pending(), "a@b.com", "wrong")


def test_a_password_accepted_outright_yields_the_authorization_code():
    # An account the provider does not challenge goes straight back to the app.
    http = FakeHttp(
        {
            "/gol/login": FakeResponse(
                302, {"location": "https://account.sainsburys.co.uk/oauth2/auth?login_verifier=v"}
            ),
            "/oauth2/auth": FakeResponse(
                302,
                {"location": "https://www.sainsburys.co.uk/gol-ui/oauth/redirect?code=AUTH1&state=s"},
            ),
        }
    )

    assert auth.submit_credentials(http, _pending(), "a@b.com", "pw") == "AUTH1"


def test_the_redirect_carrying_the_code_is_never_fetched():
    """The code is one-time; loading the page that redeems it would spend it."""
    http = FakeHttp(
        {
            "/gol/login/mfa": FakeResponse(
                302, {"location": "https://www.sainsburys.co.uk/gol-ui/oauth/redirect?code=AUTH2"}
            ),
        }
    )

    assert auth.submit_code(http, _pending(), "123456") == "AUTH2"
    fetched = [url for method, url, _ in http.calls if method == "GET"]
    assert not any("gol-ui/oauth/redirect" in url for url in fetched)


def test_a_wrong_code_comes_back_to_the_code_form():
    http = FakeHttp({"/gol/login/mfa": FakeResponse(302, {"location": "/gol/login/mfa?error=6064"})})

    with pytest.raises(AuthError, match="would not accept that code"):
        auth.submit_code(http, _pending(), "000000")


def test_the_code_is_posted_under_the_name_the_form_uses():
    http = FakeHttp(
        {"/gol/login/mfa": FakeResponse(302, {"location": "/gol-ui/oauth/redirect?code=X"})}
    )

    auth.submit_code(http, _pending(), " 123456 ")

    _, _, kwargs = http.calls[0]
    assert kwargs["data"] == {"code": "123456"}


def test_the_token_exchange_keeps_the_refresh_token():
    # Without it every expiry is another emailed code, which is the whole point.
    http = FakeHttp(
        {
            "/oauth2/token": FakeResponse(
                200,
                payload={
                    "access_token": "at",
                    "refresh_token": "rt",
                    "id_token": "it",
                    "expires_in": 3600,
                },
            )
        }
    )

    tokens = auth.exchange_code(http, "AUTH", "verifier")

    assert (tokens.access_token, tokens.refresh_token) == ("at", "rt")
    assert tokens.expires_at is not None and not tokens.expired


def test_the_exchange_sends_the_verifier_not_the_challenge():
    http = FakeHttp({"/oauth2/token": FakeResponse(200, payload={"access_token": "at"})})

    auth.exchange_code(http, "AUTH", "the-verifier")

    assert http.calls[0][2]["data"]["code_verifier"] == "the-verifier"


def test_a_refused_token_exchange_is_an_auth_error():
    http = FakeHttp({"/oauth2/token": FakeResponse(400, payload={"error": "invalid_grant"})})

    with pytest.raises(AuthError, match="refused the authorization code"):
        auth.exchange_code(http, "AUTH", "verifier")


def test_a_token_expiring_within_the_margin_counts_as_spent():
    """Refreshed early, so a request is never issued with a token that dies in flight."""
    soon = datetime.now(timezone.utc) + timedelta(seconds=auth.EXPIRY_MARGIN_S / 2)
    assert Tokens(access_token="at", expires_at=soon).expired

    later = datetime.now(timezone.utc) + timedelta(seconds=auth.EXPIRY_MARGIN_S * 4)
    assert not Tokens(access_token="at", expires_at=later).expired


def test_a_token_with_no_stated_expiry_is_believed():
    # Only reachable from a session file written before expiries were stored;
    # guessing "expired" would throw away a working refresh token for nothing.
    assert not Tokens(access_token="at").expired


def test_tokens_round_trip_through_json():
    original = Tokens(
        access_token="at",
        refresh_token="rt",
        id_token="it",
        expires_at=datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
    )

    assert Tokens.from_json(original.to_json()) == original


def test_a_session_file_without_an_access_token_is_not_tokens():
    assert Tokens.from_json({"refresh_token": "rt"}) is None
    assert Tokens.from_json(None) is None


def test_the_access_token_is_traded_for_a_shopping_session():
    http = FakeHttp({"/v1/login-access-token": FakeResponse(200, payload={"wc_token": "wc"})})

    auth.establish_gol_session(http, Tokens(access_token="at"))

    (method, url, kwargs) = http.calls[0]
    assert (method, url) == ("POST", f"{auth.GOL_URL}{auth.LOGIN_ACCESS_TOKEN_PATH}")
    assert kwargs["json"] == {"access_token": "at", "food_profile_create": False}


def test_a_refused_shopping_session_is_an_auth_error():
    http = FakeHttp({"/v1/login-access-token": FakeResponse(401)})

    with pytest.raises(AuthError, match="would not open a shopping session"):
        auth.establish_gol_session(http, Tokens(access_token="at"))


def test_a_blank_commerce_token_is_a_failed_login_not_a_success():
    """The endpoint reports a token it will not trade with a 200 and an empty
    ``wc_token``; taking that for success leaves a session that shops as a guest."""
    http = FakeHttp({"/v1/login-access-token": FakeResponse(200, payload={"wc_token": ""})})

    with pytest.raises(AuthError, match="issued no shopping session"):
        auth.establish_gol_session(http, Tokens(access_token="at"))


# -- asking for the code ------------------------------------------------------


def test_landing_on_the_mfa_page_is_not_the_same_as_a_code_being_sent():
    """The bug that made the first real login impossible to finish.

    Sainsbury's redirects to the code form, but dispatches the code from that
    page's JavaScript. A client that stops at the redirect waits forever for a
    code nobody asked for — and the page looks identical either way, so nothing
    reports the failure.
    """
    http = FakeHttp(
        {
            "/gol/login/send-mfa": FakeResponse(200, payload={"success": True}),
            "/gol/login/mfa": FakeResponse(200),
        }
    )

    auth.request_code(http)

    posted = [url for method, url, _ in http.calls if method == "POST"]
    assert posted == [f"{auth.IDENTITY_URL}{auth.SEND_MFA_PATH}"]


def test_a_refused_send_is_raised_rather_than_waited_out():
    http = FakeHttp(
        {"/gol/login/send-mfa": FakeResponse(500), "/gol/login/mfa": FakeResponse(200)}
    )

    with pytest.raises(AuthError, match="would not send a login code"):
        auth.request_code(http)


def test_a_success_false_answer_is_not_treated_as_a_sent_code():
    http = FakeHttp(
        {
            "/gol/login/send-mfa": FakeResponse(200, payload={"success": False}),
            "/gol/login/mfa": FakeResponse(200),
        }
    )

    with pytest.raises(AuthError, match="declined to send"):
        auth.request_code(http)
