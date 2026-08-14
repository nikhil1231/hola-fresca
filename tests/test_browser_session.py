"""Surviving a browser that dies mid-scrape.

The bug this covers, from a real Sainsbury's run: 179 searches succeeded, Chrome
closed, and the remaining 71 worklist rows were each recorded as their own error
without ever being tried — one crash, seventy-one false failures, and a worklist
that looked permanently broken rather than merely interrupted.
"""
from __future__ import annotations

import pytest

from app.scraper.products.browser import BrowserSession, is_dead_browser

DEAD = "Page.evaluate: Target page, context or browser has been closed"


class FakeClient:
    """A client that answers ``calls_before_death`` times, then plays dead."""

    def __init__(self, calls_before_death: int | None = None):
        self.calls_before_death = calls_before_death
        self.calls = 0
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True

    def search(self, term):
        self.calls += 1
        if self.calls_before_death is not None and self.calls > self.calls_before_death:
            raise RuntimeError(DEAD)
        return f"results for {term}"


def _session(clients, **kwargs):
    made = iter(clients)
    return BrowserSession(lambda: next(made), **kwargs)


def test_tells_a_dead_browser_from_a_failed_request():
    # Who is to blame decides whose row gets marked bad.
    assert is_dead_browser(RuntimeError(DEAD))
    assert is_dead_browser(RuntimeError("Target crashed"))
    assert not is_dead_browser(RuntimeError("https://x/y returned 404 text/html"))
    assert not is_dead_browser(RuntimeError("returned non-JSON content"))


def test_the_browser_is_launched_lazily_and_once():
    client = FakeClient()
    with _session([client]) as browser:
        assert not client.entered
        browser.call(lambda c: c.search("beans"))
        browser.call(lambda c: c.search("rice"))
    assert client.calls == 2
    assert client.exited


def test_a_dead_browser_is_relaunched_and_the_call_retried():
    first, second = FakeClient(calls_before_death=1), FakeClient()
    with _session([first, second]) as browser:
        assert browser.call(lambda c: c.search("beans")) == "results for beans"
        # The second call kills the first client; the retry lands on a new one.
        assert browser.call(lambda c: c.search("rice")) == "results for rice"

    assert browser.restarts == 1
    assert first.exited, "the dead browser should be closed, not leaked"
    assert second.calls == 1


def test_a_request_failure_is_not_retried():
    # Relaunching Chrome for a 404 would cost a browser launch per bad product.
    calls = []

    def boom(_client):
        calls.append(1)
        raise RuntimeError("https://x/y returned 404 text/html")

    with _session([FakeClient()]) as browser:
        with pytest.raises(RuntimeError, match="404"):
            browser.call(boom)

    assert len(calls) == 1
    assert browser.restarts == 0


def test_a_retry_that_also_dies_gives_up_rather_than_looping():
    # One relaunch per call. If the fresh browser dies on the same request too,
    # something is wrong with the request or the machine, not with that browser.
    clients = [FakeClient(calls_before_death=0) for _ in range(5)]
    with _session(clients, max_restarts=3) as browser:
        with pytest.raises(RuntimeError, match="closed"):
            browser.call(lambda c: c.search("beans"))

    assert browser.restarts == 1
    assert sum(1 for c in clients if c.entered) == 2, "the original plus one relaunch"


def test_restarts_are_capped_across_the_whole_run():
    """The ceiling is per run, not per call.

    A browser that dies every twentieth row is worth relaunching; one that dies
    on every row is not, and without a cap it would spawn Chrome once per
    remaining item in the worklist.
    """
    # Each client survives one call, then dies on its second — so every other
    # call costs a restart.
    clients = [FakeClient(calls_before_death=1) for _ in range(10)]
    with _session(clients, max_restarts=2) as browser:
        for term in ("beans", "rice", "pasta"):
            browser.call(lambda c, t=term: c.search(t))

        assert browser.restarts == 2, "the cap is now reached"
        with pytest.raises(RuntimeError, match="closed"):
            browser.call(lambda c: c.search("lentils"))

    assert browser.restarts == 2, "no relaunch once the cap is spent"
    assert sum(1 for c in clients if c.entered) == 3, "one initial launch plus two restarts"


def test_closing_a_browser_that_is_already_broken_is_not_an_error():
    class Stubborn(FakeClient):
        def __exit__(self, *exc):
            raise RuntimeError("context is already gone")

    client = Stubborn(calls_before_death=0)
    with _session([client, FakeClient()]) as browser:
        # The relaunch must not be derailed by the corpse refusing to be buried.
        assert browser.call(lambda c: c.search("beans")) == "results for beans"
    assert browser.restarts == 1
