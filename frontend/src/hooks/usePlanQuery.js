import { useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { fetchPlan } from '../api/planClient.js'

// The plan is one query, read by three hooks: the week's recipes, the pack/snap
// choices, and the "already own this" ticks. They were three localStorage keys
// and could drift; one query means a page cannot render a week's basket
// decisions against a different week's recipes.

export const PLAN_KEY = ['plan']

const EMPTY_WEEK = Object.freeze({
  recipes: [],
  packOverrides: {},
  snapOverrides: {},
  ownedItemKeys: [],
})

const EMPTY_PLAN = Object.freeze({ weeks: {} })

/** Server shape (a list of weeks) to lookup shape (keyed by week start). */
export function normalizePlan(data) {
  const weeks = {}
  for (const week of data?.weeks ?? []) {
    if (!week?.week_start) continue
    weeks[week.week_start] = {
      recipes: (week.recipes ?? [])
        .filter((entry) => entry?.recipe?.id != null)
        .map((entry) => ({
          recipe: entry.recipe,
          portions: entry.portions,
          protein: entry.protein ?? null,
          addedAt: entry.added_at ?? null,
        })),
      packOverrides: week.pack_overrides ?? {},
      snapOverrides: week.snap_overrides ?? {},
      ownedItemKeys: week.owned_item_keys ?? [],
    }
  }
  return { weeks }
}

export function usePlan() {
  return useQuery({
    queryKey: PLAN_KEY,
    queryFn: fetchPlan,
    select: normalizePlan,
    // Long, because every write plants its result straight into the cache: a
    // refetch would only ever confirm what the response already said. The window
    // this leaves open is another device having changed the plan, which the
    // refetch-on-focus default covers.
    staleTime: 5 * 60_000,
  })
}

export function usePlanWeek(weekStart) {
  const { data } = usePlan()
  return (weekStart && (data ?? EMPTY_PLAN).weeks[weekStart]) || EMPTY_WEEK
}

/** Replace one week in the cached plan with what a write just returned.
 *
 *  Every week-scoped write returns the whole week, so the response is planted
 *  rather than triggering a refetch — and planting only that week means a
 *  concurrent edit to a different week is not rolled back by it. */
export function usePlantWeek() {
  const queryClient = useQueryClient()
  return useCallback(
    (week) => {
      if (!week?.week_start) return
      queryClient.setQueryData(PLAN_KEY, (current) => {
        const weeks = (current?.weeks ?? []).filter((w) => w.week_start !== week.week_start)
        return { ...current, weeks: [...weeks, week].sort((a, b) => a.week_start.localeCompare(b.week_start)) }
      })
    },
    [queryClient],
  )
}

/** Apply a local change to one week immediately, and hand back an undo.
 *
 *  A plan used to live in the browser, so adding a recipe was instant. Waiting
 *  for a round trip to see the click land would be a real regression, so the
 *  change is shown at once and rolled back if the write fails. */
export function useOptimisticWeek() {
  const queryClient = useQueryClient()
  return useCallback(
    async (weekStart, update) => {
      await queryClient.cancelQueries({ queryKey: PLAN_KEY })
      const previous = queryClient.getQueryData(PLAN_KEY)
      queryClient.setQueryData(PLAN_KEY, (current) => {
        const weeks = current?.weeks ?? []
        const existing = weeks.find((w) => w.week_start === weekStart) ?? {
          week_start: weekStart,
          recipes: [],
          pack_overrides: {},
          snap_overrides: {},
          owned_item_keys: [],
        }
        const next = update(existing)
        return {
          ...current,
          weeks: [...weeks.filter((w) => w.week_start !== weekStart), next].sort((a, b) =>
            a.week_start.localeCompare(b.week_start),
          ),
        }
      })
      return () => queryClient.setQueryData(PLAN_KEY, previous)
    },
    [queryClient],
  )
}
