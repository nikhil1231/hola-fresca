"""The shopping rhythm: which weeks are being planned, and until when.

A shop repeats on a fixed cadence from an anchor week, and each shop has a
cutoff — the moment its recipe list has to be settled, some days before the week
it feeds. Everything here is pure date arithmetic over
:class:`app.db.models.PlanSettings`; the API layer owns the row and the HTTP
shape.

Times are local wall-clock throughout. A cutoff is a household deadline ("the
Saturday before, at six"), not an instant on a global timeline, and the machine
serving the app is the one the household lives in — so a naive local datetime is
the honest representation rather than a UTC stamp pretending to precision it
does not have.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

# Weeks start on Monday, matching the frontend's plan keys.
WEEK_START_WEEKDAY = 0

DATE_FMT = "%Y-%m-%d"
TIME_FMT = "%H:%M"

MIN_CADENCE_WEEKS = 1
MAX_CADENCE_WEEKS = 8
MIN_HORIZON_WEEKS = 1
MAX_HORIZON_WEEKS = 12
# A cutoff may sit up to a fortnight ahead of the week it settles, which is as
# far back as any sane cadence needs.
MAX_CUTOFF_DAYS_BEFORE = 14

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_SKIPPED = "skipped"
STATUS_PAUSED = "paused"


def parse_date(value: str) -> date:
    """Parse a ``YYYY-MM-DD`` string, raising :class:`ValueError` if malformed."""
    return datetime.strptime(value, DATE_FMT).date()


def format_date(value: date) -> str:
    return value.strftime(DATE_FMT)


def parse_time(value: str) -> time:
    return datetime.strptime(value, TIME_FMT).time()


def is_week_start(value: date) -> bool:
    return value.weekday() == WEEK_START_WEEKDAY


def week_start_for(day: date) -> date:
    """The start of the week ``day`` falls in."""
    return day - timedelta(days=(day.weekday() - WEEK_START_WEEKDAY) % 7)


def upcoming_week_start(today: date | None = None) -> date:
    """The soonest week that can still be shopped for.

    Today's own week while it is still Monday, the next one otherwise — the same
    rule the frontend's plan keys have always used.
    """
    today = today or date.today()
    return today + timedelta(days=(WEEK_START_WEEKDAY - today.weekday()) % 7)


def cutoff_at(week_start: date, *, days_before: int, at: time) -> datetime:
    return datetime.combine(week_start - timedelta(days=days_before), at)


def cycle_week_starts(
    anchor: date,
    *,
    cadence_weeks: int,
    count: int,
    today: date | None = None,
) -> list[date]:
    """The next ``count`` shop weeks on the cadence, starting from now.

    The anchor fixes the *phase*, not the start: an anchor in the past is stepped
    forward by whole cadences, so a fortnightly rhythm set up in March still
    lands on the same alternate weeks in August.
    """
    first_available = upcoming_week_start(today)
    step = timedelta(weeks=cadence_weeks)
    start = anchor
    if start < first_available:
        gap_weeks = (first_available - anchor).days // 7
        # Round up to a whole number of cadences.
        cycles = -(-gap_weeks // cadence_weeks)
        start = anchor + cycles * step
    return [start + index * step for index in range(count)]


@dataclass(frozen=True, slots=True)
class ScheduleWeek:
    week_start: date
    cutoff_at: datetime
    skipped: bool
    closed: bool
    status: str
    is_active: bool


def build_weeks(
    week_starts: list[date],
    *,
    skipped: set[str],
    cutoff_days_before: int,
    cutoff_time: time,
    paused: bool,
    now: datetime | None = None,
) -> list[ScheduleWeek]:
    """Decide each week's state, and which single week is the one to plan now.

    The active week is the first that is still open — not skipped, not past its
    cutoff — which is what makes the cutoff more than decoration: once it passes,
    attention moves on to the next shop by itself.
    """
    now = now or datetime.now()
    weeks: list[ScheduleWeek] = []
    active_found = False
    for week_start in week_starts:
        cutoff = cutoff_at(week_start, days_before=cutoff_days_before, at=cutoff_time)
        is_skipped = format_date(week_start) in skipped
        closed = cutoff <= now
        if paused:
            status = STATUS_PAUSED
        elif is_skipped:
            status = STATUS_SKIPPED
        elif closed:
            status = STATUS_CLOSED
        else:
            status = STATUS_OPEN
        is_active = status == STATUS_OPEN and not active_found
        active_found = active_found or is_active
        weeks.append(
            ScheduleWeek(
                week_start=week_start,
                cutoff_at=cutoff,
                skipped=is_skipped,
                closed=closed,
                status=status,
                is_active=is_active,
            )
        )
    return weeks
