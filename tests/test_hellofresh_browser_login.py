"""Browser escalation captures only the site's login token and closes Chromium."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error

from app.hellofresh.auth import browser_login
from app.hellofresh.client import HelloFreshError


def install_browser(monkeypatch, *, fail=False, status=200):
    state = {"closed": False, "filled": []}

    class Page:
        def set_default_timeout(self, timeout):
            pass

        def on(self, event, handler):
            self.handler = handler

        def goto(self, *args, **kwargs):
            if fail:
                raise Error("Sensitive DOM: secret-password")

        def locator(self, selector):
            self.selector = selector
            return self

        @property
        def first(self):
            return self

        def is_visible(self):
            return False

        def fill(self, value):
            state["filled"].append(value)

        def click(self):
            # Ignore matching paths on another host and responses to a GET.
            for host, method, access in [("other.example", "POST", "wrong-host"),
                                         ("www.hellofresh.co.uk", "GET", "wrong-method"),
                                         ("www.hellofresh.co.uk", "POST", "access")]:
                self.handler(SimpleNamespace(
                    url=f"https://{host}/gw/login?country=GB", status=status,
                    request=SimpleNamespace(method=method),
                    json=lambda token=access: {"access_token": token, "refresh_token": "refresh"}))

    page = Page()
    browser = SimpleNamespace(
        new_context=lambda **kwargs: SimpleNamespace(new_page=lambda: page),
        close=lambda: state.update(closed=True))

    @contextmanager
    def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))

    monkeypatch.setattr("playwright.sync_api.sync_playwright", playwright)
    return state


def test_browser_captures_login_response_and_closes(monkeypatch):
    state = install_browser(monkeypatch)
    assert browser_login("me@example.com", "secret-password") == {
        "access_token": "access", "refresh_token": "refresh"}
    assert state == {"closed": True, "filled": ["me@example.com", "secret-password"]}


def test_browser_errors_close_browser_and_hide_dom_secrets(monkeypatch):
    state = install_browser(monkeypatch, fail=True)
    with pytest.raises(HelloFreshError) as error:
        browser_login("me@example.com", "secret-password")
    assert state["closed"] and "secret-password" not in str(error.value)


def test_a_challenged_browser_login_stops_and_explains_recovery(monkeypatch):
    state = install_browser(monkeypatch, status=403)
    with pytest.raises(HelloFreshError, match="blocked the browser login.*refresh token"):
        browser_login("me@example.com", "secret-password")
    assert state["closed"]
