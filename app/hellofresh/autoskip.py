"""Skip every HelloFresh week once, the first time it is ever seen.

The default is no box. HelloFresh's delivery window rolls forward, gaining a week
at its far edge as one falls off the near end, and a new week arrives ``RUNNING``:
left alone it becomes a delivery and a charge. So this runs weekly and skips what
it has not seen before.

What it must not do is skip a week twice. HelloFresh records only ``RUNNING`` or
``PAUSED``, never who set it, so a week deliberately un-skipped looks exactly
like a week that has just appeared, and a job that skipped everything running
would quietly cancel that choice a few days later.
:class:`app.db.models.HelloFreshWeekDecision` is the half HelloFresh does not
keep: one row per week already settled, and a week with a row is never touched
again. Un-skip wherever is convenient -- here, the HelloFresh app, their website
-- and it stays un-skipped.

A week found already paused is recorded without a write, which is the subtle
half of the rule rather than tidiness: without that row, the first un-skip of
that week would read as new and be skipped straight back.

A week that comes back to ``RUNNING`` after being settled is left alone and
reported, because the reason is not knowable from here -- somebody un-skipped it,
or HelloFresh moved the plan -- and acting on a guess is how an automation
either delivers an unwanted box or cancels a wanted one. The caller decides what
to do with that (the deploy wrapper sends it on).

The deliveries list is read directly rather than through
:func:`app.api.hellofresh.coming_boxes`, which also fetches each running week's
menu: a request per week for recipe names no decision here depends on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.db.models import HelloFreshWeekDecision, RetailerAccount, User
from app.hellofresh import boxes, changes
from app.hellofresh.client import PAUSED, HelloFreshError, NeedsLogin

log = logging.getLogger("holafresca.hellofresh")

#: How far ahead to look. HelloFresh currently answers with about ten weeks
#: whatever is asked for; the ceiling only has to be past its far edge.
WINDOW_WEEKS = 12

SKIPPED = "skipped"
ALREADY_SKIPPED = "already_skipped"
LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class Decision:
    account: str | None
    week: str
    outcome: str


@dataclass
class Report:
    dry_run: bool = False
    #: Weeks settled on this run, in the order they were decided.
    decided: list[Decision] = field(default_factory=list)
    #: Settled weeks found running again: not acted on, worth a person's eye.
    resurfaced: list[Decision] = field(default_factory=list)
    #: Accounts that could not be read or written, one line each.
    errors: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> list[Decision]:
        return [d for d in self.decided if d.outcome == SKIPPED]

    def lines(self) -> list[str]:
        """The run as text, one line per thing that happened."""
        skipped, noted = ("would skip", "would note") if self.dry_run else ("skipped", "noted")
        out = [f"{skipped} {d.account} {d.week}" for d in self.skipped]
        out += [f"{noted} {d.account} {d.week} ({d.outcome})"
                for d in self.decided if d.outcome != SKIPPED]
        out += [f"RUNNING AGAIN {d.account} {d.week} - left alone" for d in self.resurfaced]
        out += [f"ERROR {e}" for e in self.errors]
        return out


def run(session: Session, *, dry_run: bool = False) -> Report:
    """Settle every unseen week on every HelloFresh account the env file can sign in."""
    report = Report(dry_run=dry_run)
    for user in session.scalars(select(User).order_by(User.id)).all():
        if (user.email or "").lower() not in config.HELLOFRESH_LOGINS:
            continue
        account = _signed_in(session, user)
        if account is None:
            report.errors.append(f"{(user.email or '').lower()}: HelloFresh would not sign in")
            continue
        try:
            _settle(session, account, report, dry_run=dry_run)
        except (HelloFreshError, NeedsLogin, httpx.HTTPError) as exc:
            report.errors.append(f"{account.email}: {exc}")
    return report


def _signed_in(session: Session, user: User) -> RetailerAccount | None:
    # Imported here, not at module scope: the API package pulls in FastAPI, and
    # this module is also the entry point of a one-shot command.
    from app.api.hellofresh import configured_login

    return configured_login(session, user)


def _settle(session: Session, account: RetailerAccount, report: Report, *, dry_run: bool) -> None:
    from app.api.hellofresh import client_for

    client = client_for(account)
    start, end = boxes.week_range(date.today(), WINDOW_WEEKS)
    settled = _settled_weeks(session, account)

    for box in boxes.summarise(client.deliveries(start, end)):
        if box.week is None:
            continue
        if box.week in settled:
            if not box.skipped and box.changeable:
                report.resurfaced.append(Decision(account.email, box.week, "running_again"))
            continue
        if box.skipped:
            outcome = ALREADY_SKIPPED
        elif not box.changeable:
            outcome = LOCKED
        elif dry_run:
            outcome = SKIPPED
        else:
            outcome = _skip(client, account, box, report)
            if outcome is None:  # refused or failed: no row, so the next run retries
                continue
        if not dry_run:  # a dry run settles nothing, including the weeks it only noted
            _record(session, account, box, outcome)
        report.decided.append(Decision(account.email, box.week, outcome))


def _skip(client, account: RetailerAccount, box: boxes.Box, report: Report) -> str | None:
    if box.subscription_id is None:
        report.errors.append(f"{account.email} {box.week}: no subscription on the delivery")
        return None
    try:
        change = changes.set_week(client, box.subscription_id, box.week, PAUSED)
    except (HelloFreshError, httpx.HTTPError) as exc:
        report.errors.append(f"{account.email} {box.week}: {exc}")
        return None
    if change.refused:
        report.errors.append(f"{account.email} {box.week}: {change.refused}")
        return None
    log.info("hellofresh: skipped %s for %s", box.week, account.email)
    return SKIPPED


def _settled_weeks(session: Session, account: RetailerAccount) -> set[str]:
    return set(session.scalars(
        select(HelloFreshWeekDecision.week).where(HelloFreshWeekDecision.account_id == account.id)
    ).all())


def _record(session: Session, account: RetailerAccount, box: boxes.Box, outcome: str) -> None:
    session.add(HelloFreshWeekDecision(account_id=account.id, week=box.week,
                                       subscription_id=box.subscription_id, outcome=outcome))
    session.commit()


__all__ = ["ALREADY_SKIPPED", "Decision", "LOCKED", "Report", "SKIPPED", "WINDOW_WEEKS", "run"]
