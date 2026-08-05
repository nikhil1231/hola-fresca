import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  fetchSchedule,
  setWeekSkipped,
  updateScheduleSettings,
} from '../api/scheduleClient.js'
import { getUpcomingWeekStart } from './useWeeklyPlan.js'

const SCHEDULE_KEY = ['schedule']

// A write reshapes the whole schedule, and how much history each caller is
// holding is its own business — so the writes invalidate rather than planting
// their response, which carries only the history the *write* asked for and would
// otherwise collapse an expanded page back to none.
function useRefreshSchedule() {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: SCHEDULE_KEY })
}

// Mirrors MAX_PAST_WEEKS in app/schedule.py: asking for more than the server
// will serve is a 422, so the client stops short of it rather than finding out.
export const MAX_PAST_WEEKS = 26

export function useSchedule(pastWeeks = 0) {
  return useQuery({
    queryKey: [...SCHEDULE_KEY, pastWeeks],
    queryFn: () => fetchSchedule(pastWeeks),
    // The cutoff turns a week from open to closed with nothing else changing, so
    // a long-lived tab has to re-ask rather than trust what it was told at load.
    staleTime: 60_000,
    refetchInterval: 5 * 60_000,
    // Asking for more history should extend the page, not blank it.
    placeholderData: (previous) => previous,
  })
}

/** The schedule including all the history the server will serve.
 *
 *  For pages that take a week from the URL. The planning window holds only the
 *  weeks still to be shopped for, so without the history a link to a finished
 *  week resolves to nothing and quietly opens the week being planned instead —
 *  which is how the basket for a shop three weeks ago used to show next week's. */
export function useScheduleWithHistory() {
  return useSchedule(MAX_PAST_WEEKS)
}

export function useUpdateScheduleSettings() {
  const refresh = useRefreshSchedule()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: updateScheduleSettings,
    onSuccess: () => {
      refresh()
      queryClient.invalidateQueries({ queryKey: ['planner-basket'] })
    },
  })
}

export function useSetWeekSkipped() {
  const refresh = useRefreshSchedule()
  return useMutation({ mutationFn: setWeekSkipped, onSuccess: refresh })
}

export const WEEK_STATUS_LABELS = {
  open: 'Planning',
  closed: 'Cutoff passed',
  skipped: 'Skipped',
  paused: 'Paused',
}

/** Whether a week has been and gone: its shop is a record, not a draft.
 *
 *  The boundary is the week being planned, not today, so the week currently
 *  being cooked counts as past from the Tuesday — which is right, because its
 *  shop was bought before it started. */
export function isPastWeekStart(weekStart) {
  return Boolean(weekStart) && weekStart < getUpcomingWeekStart()
}

function isCompleteWeek(weekStart) {
  const end = new Date(`${weekStart}T00:00:00`)
  end.setDate(end.getDate() + 7)
  return end.getTime() <= Date.now()
}

/** A finished week the schedule does not list — off the current cadence, or
 *  older than the history that was fetched. It still has recipes and a basket,
 *  so it stands as a week of its own rather than resolving to nothing. */
function historicWeek(weekStart) {
  return {
    week_start: weekStart,
    cutoff_at: null,
    status: 'closed',
    skipped: false,
    closed: true,
    complete: isCompleteWeek(weekStart),
    is_active: false,
  }
}

/** The week a page should act on: an explicit choice if it can be placed at all,
 *  otherwise whichever week is currently being planned.
 *
 *  A requested week is looked for in the planning window, then in the history,
 *  and failing both is taken at face value if it is in the past. Only a week
 *  that is neither planned nor over falls through to the active one — asking for
 *  a finished week and being handed next week's basket is not a fallback, it is
 *  the wrong answer to a question that had a right one. */
export function resolveTargetWeek(schedule, requestedWeekStart) {
  const weeks = schedule?.weeks ?? []
  const requested = weeks.find((week) => week.week_start === requestedWeekStart)
  if (requested) return requested
  const past = (schedule?.past_weeks ?? []).find(
    (week) => week.week_start === requestedWeekStart,
  )
  if (past) return past
  if (isPastWeekStart(requestedWeekStart)) return historicWeek(requestedWeekStart)
  const active = weeks.find((week) => week.is_active)
  if (active) return active
  return weeks[0] ?? null
}

const dayFormat = new Intl.DateTimeFormat(undefined, {
  weekday: 'short',
  day: 'numeric',
  month: 'short',
})

const cutoffFormat = new Intl.DateTimeFormat(undefined, {
  weekday: 'long',
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
})

/** "Mon 10 – Sun 16 Aug", the span the week's cooking actually covers. */
export function formatWeekRange(weekStart) {
  const start = new Date(`${weekStart}T00:00:00`)
  const end = new Date(start)
  end.setDate(end.getDate() + 6)
  return `${dayFormat.format(start)} – ${dayFormat.format(end)}`
}

/** "Mon 10 Aug", a compact label for places where the start identifies a week. */
export function formatWeekStart(weekStart) {
  const start = new Date(`${weekStart}T00:00:00`)
  return dayFormat.format(start)
}

export function formatCutoff(cutoffAt) {
  if (!cutoffAt) return null
  const date = new Date(cutoffAt)
  if (Number.isNaN(date.getTime())) return null
  return cutoffFormat.format(date)
}

/** How long is left to change a week's recipes, coarse on purpose. */
export function formatCutoffCountdown(cutoffAt, now = Date.now()) {
  if (!cutoffAt) return null
  const target = new Date(cutoffAt).getTime()
  if (Number.isNaN(target)) return null
  const minutes = Math.round((target - now) / 60_000)
  if (minutes <= 0) return 'closed'
  if (minutes < 60) return `${minutes} min left`
  const hours = Math.round(minutes / 60)
  if (hours < 36) return `${hours}h left`
  return `${Math.round(hours / 24)} days left`
}
