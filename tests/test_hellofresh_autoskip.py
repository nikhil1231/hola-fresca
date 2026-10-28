"""The weekly auto-skip: settle a week once, then never touch it again.

Nothing reaches HelloFresh. The fake account answers the two calls the job makes
-- the deliveries list and the per-week read/PATCH -- so these pin the rule
rather than HelloFresh's behaviour: what gets written, what deliberately does
not, and which weeks are recorded as settled.
"""
from __future__ import annotations

import pytest

from app import config
from app.api import hellofresh
from app.db import retailer_accounts
from app.db.models import HelloFreshWeekDecision, User
from app.hellofresh import autoskip
from app.hellofresh.client import HelloFreshError

SUBSCRIPTION = "7"


class FakeAccount:
    """A HelloFresh subscription holding a handful of weeks."""

    def __init__(self, weeks: dict[str, str], locked: set[str] | None = None, fail: set[str] | None = None):
        self.weeks = dict(weeks)
        self.locked = locked or set()
        self.fail = fail or set()
        self.patched: list[tuple[str, str]] = []

    def _delivery(self, week: str) -> dict:
        return {"id": week, "subscriptionId": SUBSCRIPTION, "status": self.weeks[week],
                "cutoffDate": "2030-01-01T23:59:59+0000", "deliveryDate": "2030-01-06T12:00:00+0000",
                "allowedActions": {"pause": week not in self.locked}}

    def deliveries(self, start: str, end: str) -> dict:
        return {"items": [self._delivery(week) for week in sorted(self.weeks)]}

    def delivery(self, subscription_id: str, week: str) -> dict:
        return {"delivery": self._delivery(week)}

    def set_delivery_status(self, subscription_id, week, status, *, cutoff_date, delivery_date):
        if week in self.fail:
            raise HelloFreshError("HelloFresh said no")
        self.patched.append((week, status))
        self.weeks[week] = status


@pytest.fixture
def connected(factory, monkeypatch):
    """One signed-in HelloFresh account, and a hook to give it weeks."""
    monkeypatch.setattr(config, "HELLOFRESH_LOGINS", {"me@x.com": ("box@x.com", "pw", None)})
    with factory() as session:
        me = session.query(User).order_by(User.id).first()
        me.email = "me@x.com"
        session.commit()
        account = retailer_accounts.connect(session, me.id, "hellofresh", email="box@x.com")
        monkeypatch.setattr(hellofresh, "configured_login", lambda s, u, **kw: account)

        def hold(fake: FakeAccount) -> FakeAccount:
            monkeypatch.setattr(hellofresh, "client_for", lambda a: fake)
            return fake

        yield session, account, hold


def settled(session, account) -> dict[str, str]:
    rows = session.query(HelloFreshWeekDecision).filter_by(account_id=account.id).all()
    return {row.week: row.outcome for row in rows}


def test_an_unseen_running_week_is_skipped_once_and_then_left_alone(connected):
    session, account, hold = connected
    fake = hold(FakeAccount({"2030-W01": "RUNNING", "2030-W02": "RUNNING"}))

    first = autoskip.run(session)
    assert fake.patched == [("2030-W01", "PAUSED"), ("2030-W02", "PAUSED")]
    assert [d.week for d in first.skipped] == ["2030-W01", "2030-W02"]
    assert settled(session, account) == {"2030-W01": "skipped", "2030-W02": "skipped"}

    second = autoskip.run(session)
    assert len(fake.patched) == 2  # nothing further was sent
    assert second.skipped == [] and second.resurfaced == []


def test_a_week_un_skipped_after_the_job_settled_it_is_left_running(connected):
    session, account, hold = connected
    fake = hold(FakeAccount({"2030-W01": "RUNNING"}))
    autoskip.run(session)

    fake.weeks["2030-W01"] = "RUNNING"  # un-skipped on HelloFresh's own site
    report = autoskip.run(session)

    assert len(fake.patched) == 1  # the job did not take it back
    assert [d.week for d in report.resurfaced] == ["2030-W01"]
    assert fake.weeks["2030-W01"] == "RUNNING"


def test_a_week_already_paused_is_recorded_without_a_write(connected):
    """The subtle half of the rule: that row is what protects a later un-skip."""
    session, account, hold = connected
    fake = hold(FakeAccount({"2030-W01": "PAUSED"}))

    autoskip.run(session)
    assert fake.patched == []
    assert settled(session, account) == {"2030-W01": "already_skipped"}

    fake.weeks["2030-W01"] = "RUNNING"  # the box is wanted after all
    report = autoskip.run(session)
    assert fake.patched == []  # and it is not skipped straight back
    assert [d.week for d in report.resurfaced] == ["2030-W01"]


def test_a_week_past_its_cutoff_is_recorded_and_not_attempted(connected):
    session, account, hold = connected
    fake = hold(FakeAccount({"2030-W01": "RUNNING"}, locked={"2030-W01"}))

    report = autoskip.run(session)

    assert fake.patched == []
    assert settled(session, account) == {"2030-W01": "locked"}
    assert report.skipped == [] and report.errors == []


def test_a_dry_run_changes_nothing_and_records_nothing(connected):
    """Including the weeks it only notes: a row written here would settle a week
    that was never actually looked at by a real run."""
    session, account, hold = connected
    fake = hold(FakeAccount({"2030-W01": "RUNNING", "2030-W02": "PAUSED", "2030-W03": "RUNNING"},
                            locked={"2030-W03"}))

    report = autoskip.run(session, dry_run=True)

    assert fake.patched == []
    assert settled(session, account) == {}
    assert [d.week for d in report.skipped] == ["2030-W01"]
    assert report.lines()[0].startswith("would skip")
    assert any("would note" in line for line in report.lines())

    # ...so a real run afterwards still has every one of them to settle.
    autoskip.run(session)
    assert settled(session, account) == {"2030-W01": "skipped", "2030-W02": "already_skipped",
                                        "2030-W03": "locked"}


def test_a_failed_skip_leaves_no_row_so_the_next_run_tries_again(connected):
    session, account, hold = connected
    fake = hold(FakeAccount({"2030-W01": "RUNNING"}, fail={"2030-W01"}))

    report = autoskip.run(session)
    assert settled(session, account) == {}
    assert report.errors and "2030-W01" in report.errors[0]

    fake.fail.clear()
    autoskip.run(session)
    assert fake.patched == [("2030-W01", "PAUSED")]
    assert settled(session, account) == {"2030-W01": "skipped"}


def test_an_account_that_will_not_sign_in_is_an_error_not_a_silent_pass(connected, monkeypatch):
    session, _, _ = connected
    monkeypatch.setattr(hellofresh, "configured_login", lambda s, u, **kw: None)

    report = autoskip.run(session)

    assert report.errors == ["me@x.com: HelloFresh would not sign in"]
    assert report.decided == []
