"""Verification cookies must be obtained before sending the password request."""
from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error

from app.hellofresh import auth
from app.hellofresh.client import HelloFreshError


def install_browser(monkeypatch, *, fail=False, status=200):
    state = {"closed": False, "waits": 0, "requests": []}

    class Page:
        def set_default_timeout(self, timeout):
            pass

        def route(self, url, handler):
            self.handler = handler

        def goto(self, *args, **kwargs):
            if fail:
                raise Error("Sensitive DOM: secret-password")
            upstream = SimpleNamespace(text=lambda: '''<html><script>analytics()</script>
                <script>window.__CF$cv$params={};load('/cdn-cgi/challenge-platform/scripts/jsd/main.js')</script></html>''')
            self.handler(SimpleNamespace(fetch=lambda **kwargs: upstream,
                                         fulfill=lambda **kwargs: state.update(document=kwargs["body"])))

        def wait_for_timeout(self, timeout):
            state["waits"] += 1

        def evaluate(self, script, arguments):
            assert state["waits"] == 1  # verification has completed before credentials are sent
            state["requests"].append(arguments)
            return {"status": status, "token": {"access_token": "access", "refresh_token": "refresh"}}

    page = Page()
    context = SimpleNamespace(new_page=lambda: page,
                              cookies=lambda: [{"name": "cf_clearance"}] if state["waits"] else [])
    browser = SimpleNamespace(new_context=lambda **kwargs: context,
                              close=lambda: state.update(closed=True))

    @contextmanager
    def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))

    @contextmanager
    def display():
        yield {"headless": True}

    monkeypatch.setattr("playwright.sync_api.sync_playwright", playwright)
    monkeypatch.setattr(auth, "display_options", display)
    return state


def test_browser_verifies_before_login_and_closes(monkeypatch):
    state = install_browser(monkeypatch)
    assert auth.browser_login("me@example.com", "secret-password") == {
        "access_token": "access", "refresh_token": "refresh"}
    assert state["closed"]
    assert state["requests"] == [["GB", "en-GB", "me@example.com", "secret-password"]]
    assert "analytics()" not in state["document"]
    assert "/cdn-cgi/challenge-platform/" in state["document"]
    assert "secret-password" not in state["document"]


def test_browser_errors_close_browser_and_hide_dom_secrets(monkeypatch):
    state = install_browser(monkeypatch, fail=True)
    with pytest.raises(HelloFreshError) as error:
        auth.browser_login("me@example.com", "secret-password")
    assert state["closed"] and "secret-password" not in str(error.value)


def test_a_challenged_browser_login_stops_and_explains_recovery(monkeypatch):
    state = install_browser(monkeypatch, status=403)
    with pytest.raises(HelloFreshError, match="blocked the browser login.*refresh token"):
        auth.browser_login("me@example.com", "secret-password")
    assert state["closed"]


@pytest.mark.parametrize("ready", [True, False])
def test_virtual_display_is_private_and_cleaned_up_even_on_failure(monkeypatch, ready):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(auth.shutil, "which", lambda name: "/usr/bin/Xvfb")
    state = []
    process = SimpleNamespace(stdout=StringIO("123\n"), terminate=lambda: state.append("terminated"),
                              wait=lambda **kwargs: state.append("waited"))
    monkeypatch.setattr(auth.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(auth.select, "select", lambda *args: ([process.stdout] if ready else [], [], []))
    if ready:
        with auth.display_options() as options:
            assert options["headless"] is False
            assert options["env"]["DISPLAY"] == ":123"
            assert "DISPLAY" not in auth.os.environ
    else:
        with pytest.raises(HelloFreshError, match="display did not start"):
            with auth.display_options():
                pytest.fail("Timed-out display must not be used")
    assert state == ["terminated", "waited"] and process.stdout.closed
