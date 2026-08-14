"""Stopping a broken run instead of letting it condemn its worklist.

The bug this descends from, from a real Sainsbury's run: 179 searches succeeded,
Chrome closed, and the remaining 71 worklist rows were each recorded as their own
error without ever being tried — one crash, seventy-one false failures, and a
worklist that looked permanently broken rather than merely interrupted.

The browser is gone, but the failure *shape* is not: a dropped connection walks a
worklist marking rows bad just as efficiently. ``_RunHealth`` is the rule kept
without the browser.
"""
from __future__ import annotations

import pytest

from app.scraper.products.pipeline import MAX_CONSECUTIVE_FAILURES, ScrapeAborted, _RunHealth


def test_an_isolated_failure_is_the_rows_problem_not_the_runs():
    health = _RunHealth(limit=3)
    for _ in range(20):
        health.failed(RuntimeError("one bad product"))
        health.succeeded()
    # Twenty failures, never two in a row, and the run is still going.
    assert health.consecutive == 0


def test_a_streak_of_failures_stops_the_run():
    health = _RunHealth(limit=3)
    health.failed(RuntimeError("boom"))
    health.failed(RuntimeError("boom"))

    with pytest.raises(ScrapeAborted, match="3 failures in a row"):
        health.failed(RuntimeError("boom"))


def test_the_abort_says_the_worklist_was_left_alone():
    # The message is what the operator reads before deciding whether to re-run,
    # so it has to distinguish "interrupted" from "these rows are bad".
    health = _RunHealth(limit=2)
    health.failed(RuntimeError("first"))
    with pytest.raises(ScrapeAborted) as caught:
        health.failed(RuntimeError("connection reset"))

    message = str(caught.value)
    assert "remaining worklist is untouched" in message
    assert "connection reset" in message, "the operator needs the cause, not just the count"


def test_one_success_forgives_the_streak():
    health = _RunHealth(limit=3)
    health.failed(RuntimeError("boom"))
    health.failed(RuntimeError("boom"))
    health.succeeded()
    health.failed(RuntimeError("boom"))
    health.failed(RuntimeError("boom"))
    assert health.consecutive == 2  # no abort: the run demonstrably still works


def test_the_default_limit_tolerates_the_observed_transient_rate():
    # Measured against the live shops at roughly one transient per 150 requests,
    # so the default must not be so tight that an ordinary run trips it.
    assert MAX_CONSECUTIVE_FAILURES >= 5
