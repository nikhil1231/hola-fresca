"""Shopping schedule API: the cadence, the cutoff, and which weeks are skipped.

The recipes for a week live with the rest of the plan in the browser; what is
persisted here is the rhythm they hang off. That split is deliberate: the rhythm
is a standing decision about the household (and the thing an unattended job would
have to consult), while a week's recipe list is scratch until it is pushed to the
retailer.
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schedule as sched
from app.api.deps import get_current_user, get_session
from app.api.schemas import (
    ScheduleOut,
    ScheduleSettingsIn,
    ScheduleSettingsOut,
    ScheduleWeekIn,
    ScheduleWeekOut,
)
from app.db.models import PlanSettings, PlanWeek, User

router = APIRouter(prefix="/api/schedule", tags=["schedule"])


def _settings_row(session: Session, user_id: int) -> PlanSettings:
    """This user's settings row, created with defaults the first time it is asked for.

    Created lazily rather than alongside the account: the defaults are the
    schedule until someone changes it, so a row that says exactly that is worth
    nothing until it does.
    """
    row = session.scalar(select(PlanSettings).where(PlanSettings.user_id == user_id))
    if row is not None:
        return row
    row = PlanSettings(
        user_id=user_id,
        anchor_week_start=sched.format_date(sched.upcoming_week_start()),
    )
    session.add(row)
    session.commit()
    return row


def _settings_out(row: PlanSettings) -> ScheduleSettingsOut:
    return ScheduleSettingsOut(
        cadence_weeks=row.cadence_weeks,
        anchor_week_start=row.anchor_week_start,
        cutoff_days_before=row.cutoff_days_before,
        cutoff_time=row.cutoff_time,
        paused=bool(row.paused),
        horizon_weeks=row.horizon_weeks,
        recipes_per_week=row.recipes_per_week,
        default_portions=row.default_portions,
        pack_shortfall_tolerance_pct=row.pack_shortfall_tolerance_pct,
    )


def pack_shortfall_tolerance_pct(session: Session, user_id: int) -> float:
    """This user's saved tolerance, creating the defaults if necessary."""
    return float(_settings_row(session, user_id).pack_shortfall_tolerance_pct)


def _week_out(week: sched.ScheduleWeek) -> ScheduleWeekOut:
    return ScheduleWeekOut(
        week_start=sched.format_date(week.week_start),
        cutoff_at=week.cutoff_at.isoformat(timespec="minutes"),
        status=week.status,
        skipped=week.skipped,
        closed=week.closed,
        complete=week.complete,
        is_active=week.is_active,
    )


def _require_week_start(value: str) -> date:
    try:
        parsed = sched.parse_date(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Not a date: {value}") from None
    if not sched.is_week_start(parsed):
        raise HTTPException(status_code=400, detail=f"{value} is not a Monday")
    return parsed


def _prune_past_weeks(session: Session, user_id: int, before: date) -> None:
    """Forget overrides for weeks too old to be shown at all.

    Not simply "weeks that have been and gone": history is displayed, and a week
    that reads as an ordinary empty shop when it was deliberately skipped is a
    worse record than no record. So the floor is the oldest week the schedule
    could ever hand back, which still stops the table growing a row a week
    forever.
    """
    stale = session.scalars(
        select(PlanWeek).where(
            PlanWeek.user_id == user_id,
            PlanWeek.week_start < sched.format_date(before),
        )
    ).all()
    if not stale:
        return
    for row in stale:
        session.delete(row)
    session.commit()


def _skipped_week_starts(session: Session, user_id: int) -> set[str]:
    return set(
        session.scalars(
            select(PlanWeek.week_start).where(
                PlanWeek.user_id == user_id,
                PlanWeek.skipped == True,  # noqa: E712
            )
        )
    )


def _schedule_out(
    session: Session, row: PlanSettings, past_weeks: int = 0
) -> ScheduleOut:
    now = datetime.now()
    anchor = sched.parse_date(row.anchor_week_start)
    cadence = row.cadence_weeks
    history_floor = sched.past_week_starts(
        anchor, cadence_weeks=cadence, count=sched.MAX_PAST_WEEKS, today=now.date()
    )[0]
    _prune_past_weeks(session, row.user_id, history_floor)

    skipped = _skipped_week_starts(session, row.user_id)
    cutoff_time = sched.parse_time(row.cutoff_time)

    def build(week_starts: list[date], *, paused: bool) -> list[sched.ScheduleWeek]:
        return sched.build_weeks(
            week_starts,
            skipped=skipped,
            cutoff_days_before=row.cutoff_days_before,
            cutoff_time=cutoff_time,
            paused=paused,
            now=now,
        )

    weeks = build(
        sched.cycle_week_starts(
            anchor, cadence_weeks=cadence, count=row.horizon_weeks, today=now.date()
        ),
        paused=bool(row.paused),
    )
    # Pausing is a statement about what happens next, so history is built as it
    # actually was: a shop that happened does not retroactively become paused.
    past = build(
        sched.past_week_starts(
            anchor, cadence_weeks=cadence, count=past_weeks, today=now.date()
        ),
        paused=False,
    )
    active = next((week for week in weeks if week.is_active), None)
    return ScheduleOut(
        settings=_settings_out(row),
        weeks=[_week_out(week) for week in weeks],
        past_weeks=[_week_out(week) for week in past],
        has_more_past=past_weeks < sched.MAX_PAST_WEEKS,
        active_week_start=sched.format_date(active.week_start) if active else None,
        now=now.isoformat(timespec="minutes"),
    )


@router.get("", response_model=ScheduleOut)
def get_schedule(
    past_weeks: int = Query(default=0, ge=0, le=sched.MAX_PAST_WEEKS),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ScheduleOut:
    """The planning window, plus however many finished shops were asked for."""
    return _schedule_out(session, _settings_row(session, user.id), past_weeks)


@router.get("/settings", response_model=ScheduleSettingsOut)
def get_settings(
    session: Session = Depends(get_session), user: User = Depends(get_current_user)
) -> ScheduleSettingsOut:
    return _settings_out(_settings_row(session, user.id))


@router.put("/settings", response_model=ScheduleOut)
def update_settings(
    body: ScheduleSettingsIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ScheduleOut:
    """Change the rhythm. Returns the whole schedule, since every field reshapes it."""
    row = _settings_row(session, user.id)
    values = body.model_dump(exclude_unset=True, exclude_none=True)

    if "anchor_week_start" in values:
        _require_week_start(values["anchor_week_start"])
    if "cutoff_time" in values:
        try:
            sched.parse_time(values["cutoff_time"])
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Not a time of day (HH:MM): {values['cutoff_time']}",
            ) from None

    for field, value in values.items():
        setattr(row, field, value)
    session.commit()
    return _schedule_out(session, row)


@router.put("/weeks/{week_start}", response_model=ScheduleOut)
def set_week(
    week_start: str,
    body: ScheduleWeekIn,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ScheduleOut:
    """Skip a week, or put it back."""
    parsed = _require_week_start(week_start)
    if parsed < sched.upcoming_week_start():
        raise HTTPException(status_code=400, detail=f"{week_start} has already been and gone")

    row = session.scalar(
        select(PlanWeek).where(
            PlanWeek.user_id == user.id, PlanWeek.week_start == week_start
        )
    )
    if row is None:
        row = PlanWeek(user_id=user.id, week_start=week_start)
        session.add(row)
    row.skipped = body.skipped
    row.note = body.note
    session.commit()
    return _schedule_out(session, _settings_row(session, user.id))
