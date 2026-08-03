"""Shopping schedule API: the cadence, the cutoff, and which weeks are skipped.

The recipes for a week live with the rest of the plan in the browser; what is
persisted here is the rhythm they hang off. That split is deliberate: the rhythm
is a standing decision about the household (and the thing an unattended job would
have to consult), while a week's recipe list is scratch until it is pushed to the
retailer.
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schedule as sched
from app.api.deps import get_session
from app.api.schemas import (
    ScheduleOut,
    ScheduleSettingsIn,
    ScheduleSettingsOut,
    ScheduleWeekIn,
    ScheduleWeekOut,
)
from app.db.models import PlanSettings, PlanWeek

router = APIRouter(prefix="/api/schedule", tags=["schedule"])


def _settings_row(session: Session) -> PlanSettings:
    """The one settings row, created with defaults the first time it is asked for."""
    row = session.scalar(select(PlanSettings).order_by(PlanSettings.id).limit(1))
    if row is not None:
        return row
    row = PlanSettings(anchor_week_start=sched.format_date(sched.upcoming_week_start()))
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
    )


def _week_out(week: sched.ScheduleWeek) -> ScheduleWeekOut:
    return ScheduleWeekOut(
        week_start=sched.format_date(week.week_start),
        cutoff_at=week.cutoff_at.isoformat(timespec="minutes"),
        status=week.status,
        skipped=week.skipped,
        closed=week.closed,
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


def _prune_past_weeks(session: Session, before: date) -> None:
    """Forget overrides for weeks that have been and gone.

    A skip only means anything ahead of time, and without this the table grows a
    row a week forever.
    """
    stale = session.scalars(
        select(PlanWeek).where(PlanWeek.week_start < sched.format_date(before))
    ).all()
    if not stale:
        return
    for row in stale:
        session.delete(row)
    session.commit()


def _skipped_week_starts(session: Session) -> set[str]:
    return set(session.scalars(select(PlanWeek.week_start).where(PlanWeek.skipped == True)))  # noqa: E712


def _schedule_out(session: Session, row: PlanSettings) -> ScheduleOut:
    now = datetime.now()
    _prune_past_weeks(session, sched.upcoming_week_start(now.date()))
    week_starts = sched.cycle_week_starts(
        sched.parse_date(row.anchor_week_start),
        cadence_weeks=row.cadence_weeks,
        count=row.horizon_weeks,
        today=now.date(),
    )
    weeks = sched.build_weeks(
        week_starts,
        skipped=_skipped_week_starts(session),
        cutoff_days_before=row.cutoff_days_before,
        cutoff_time=sched.parse_time(row.cutoff_time),
        paused=bool(row.paused),
        now=now,
    )
    active = next((week for week in weeks if week.is_active), None)
    return ScheduleOut(
        settings=_settings_out(row),
        weeks=[_week_out(week) for week in weeks],
        active_week_start=sched.format_date(active.week_start) if active else None,
        now=now.isoformat(timespec="minutes"),
    )


@router.get("", response_model=ScheduleOut)
def get_schedule(session: Session = Depends(get_session)) -> ScheduleOut:
    return _schedule_out(session, _settings_row(session))


@router.get("/settings", response_model=ScheduleSettingsOut)
def get_settings(session: Session = Depends(get_session)) -> ScheduleSettingsOut:
    return _settings_out(_settings_row(session))


@router.put("/settings", response_model=ScheduleOut)
def update_settings(
    body: ScheduleSettingsIn, session: Session = Depends(get_session)
) -> ScheduleOut:
    """Change the rhythm. Returns the whole schedule, since every field reshapes it."""
    row = _settings_row(session)
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
    week_start: str, body: ScheduleWeekIn, session: Session = Depends(get_session)
) -> ScheduleOut:
    """Skip a week, or put it back."""
    parsed = _require_week_start(week_start)
    if parsed < sched.upcoming_week_start():
        raise HTTPException(status_code=400, detail=f"{week_start} has already been and gone")

    row = session.scalar(select(PlanWeek).where(PlanWeek.week_start == week_start))
    if row is None:
        row = PlanWeek(week_start=week_start)
        session.add(row)
    row.skipped = body.skipped
    row.note = body.note
    session.commit()
    return _schedule_out(session, _settings_row(session))
