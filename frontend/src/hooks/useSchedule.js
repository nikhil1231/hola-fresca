import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  fetchSchedule,
  setWeekSkipped,
  updateScheduleSettings,
} from '../api/scheduleClient.js'

const SCHEDULE_KEY = ['schedule']

// Every schedule write returns the reshaped schedule, so the response is planted
// straight into the cache rather than triggering a refetch.
function usePlantSchedule() {
  const queryClient = useQueryClient()
  return (schedule) => queryClient.setQueryData(SCHEDULE_KEY, schedule)
}

export function useSchedule() {
  return useQuery({
    queryKey: SCHEDULE_KEY,
    queryFn: fetchSchedule,
    // The cutoff turns a week from open to closed with nothing else changing, so
    // a long-lived tab has to re-ask rather than trust what it was told at load.
    staleTime: 60_000,
    refetchInterval: 5 * 60_000,
  })
}

export function useUpdateScheduleSettings() {
  const plant = usePlantSchedule()
  return useMutation({ mutationFn: updateScheduleSettings, onSuccess: plant })
}

export function useSetWeekSkipped() {
  const plant = usePlantSchedule()
  return useMutation({ mutationFn: setWeekSkipped, onSuccess: plant })
}

export const WEEK_STATUS_LABELS = {
  open: 'Planning',
  closed: 'Cutoff passed',
  skipped: 'Skipped',
  paused: 'Paused',
}

/** The week a page should act on: an explicit choice if it is still in the
 *  schedule, otherwise whichever week is currently being planned. */
export function resolveTargetWeek(schedule, requestedWeekStart) {
  const weeks = schedule?.weeks ?? []
  const requested = weeks.find((week) => week.week_start === requestedWeekStart)
  if (requested) return requested
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
