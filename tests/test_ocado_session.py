"""Session transport: CSRF churn and re-auth, with the network faked."""
from __future__ import annotations

import httpx

from app.ocado.auth import AuthState
from app.ocado.session import AUTH_PROBE_PATH, OcadoSession

WRITE_PATH = "/api/cart/v1/carts/active/apply-quantity"


def basket_html(token: str) -> str:
    return f'<html><script>{{"session":{{"csrf":{{"token":"{token}"}}}}}}</script></html>'


class FakeAuth:
    """Records how it was called and hands back a working cookie."""

    def __init__(self, state=AuthState.READY):
        self.calls = []
        self.state = state

    def ensure_authenticated(self, session, *, trust_existing=True):
        self.calls.append(trust_existing)
        session.client.cookies.set("global_sid", "fresh", domain="www.ocado.com")
        return self.state


def make_session(handler, tmp_path, auth=None):
    client = httpx.Client(
        base_url="https://www.ocado.com", transport=httpx.MockTransport(handler)
    )
    return OcadoSession(
        client=client, jar_path=tmp_path / "session.json", auth=auth or FakeAuth()
    )


def test_csrf_failure_rescrapes_once_and_retries(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.headers.get("x-csrf-token")))
        if request.url.path == "/basket":
            seen = len([c for c in calls if c[1] == "/basket"])
            return httpx.Response(200, text=basket_html("new" if seen > 1 else "old"))
        if request.headers.get("x-csrf-token") == "old":
            return httpx.Response(403, headers={"ecom-csrf-failure": "true"})
        return httpx.Response(200, json={"ok": True})

    session = make_session(handler, tmp_path)
    response = session.request("POST", WRITE_PATH, json=[])

    assert response.status_code == 200
    assert calls == [
        ("GET", "/basket", None),
        ("POST", WRITE_PATH, "old"),
        ("GET", "/basket", None),
        ("POST", WRITE_PATH, "new"),
    ]


def test_401_reauths_once_and_retries(tmp_path):
    auth = FakeAuth()
    seen = 0

    def handler(request):
        nonlocal seen
        if request.url.path == AUTH_PROBE_PATH:
            return httpx.Response(200, json={})
        seen += 1
        return httpx.Response(401) if seen == 1 else httpx.Response(200, json={"ok": True})

    session = make_session(handler, tmp_path, auth)
    response = session.request("GET", "/api/cart/v2/carts/active/cart-view")

    assert response.status_code == 200
    assert seen == 2
    # The 401 is proof the jar is dead, so the ladder must not re-trust it.
    assert auth.calls == [False]


def test_csrf_is_refetched_after_a_reauth(tmp_path):
    """A new login means a new session, and the CSRF token dies with the old one."""
    auth = FakeAuth()
    tokens = []
    writes = 0

    def handler(request):
        nonlocal writes
        if request.url.path == "/basket":
            return httpx.Response(200, text=basket_html(f"token-{len(tokens)}"))
        if request.url.path == AUTH_PROBE_PATH:
            return httpx.Response(200, json={})
        writes += 1
        tokens.append(request.headers.get("x-csrf-token"))
        return httpx.Response(401) if writes == 1 else httpx.Response(200, json={"ok": True})

    session = make_session(handler, tmp_path, auth)
    response = session.request("POST", WRITE_PATH, json=[])

    assert response.status_code == 200
    assert tokens == ["token-0", "token-1"], "retry must not reuse the pre-login token"


def test_probe_reports_a_stale_jar_as_unauthenticated(tmp_path):
    def handler(request):
        return httpx.Response(401, json={"code": "UNAUTHORIZED"})

    session = make_session(handler, tmp_path)
    session.client.cookies.set("global_sid", "stale", domain="www.ocado.com")

    # The cookie is present but dead; presence alone must not read as logged in.
    assert session.has_auth_cookies() is True
    assert session.probe_authenticated() is False


def test_probe_skips_the_request_with_no_login_cookie(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    session = make_session(handler, tmp_path)
    session.client.cookies.set("aws-waf-token", "x", domain="www.ocado.com")

    # A WAF token is not a login cookie.
    assert session.probe_authenticated() is False
    assert calls == []


def test_cookies_and_token_survive_a_reload(tmp_path):
    def handler(request):
        if request.url.path == "/basket":
            return httpx.Response(200, text=basket_html("persisted"))
        return httpx.Response(200, json={})

    session = make_session(handler, tmp_path)
    session.client.cookies.set("global_sid", "abc", domain="www.ocado.com")
    assert session.csrf() == "persisted"
    session.save()

    reloaded = make_session(handler, tmp_path)
    assert reloaded.csrf() == "persisted"
    assert reloaded.has_auth_cookies() is True
