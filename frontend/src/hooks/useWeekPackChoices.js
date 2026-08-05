import { useCallback } from 'react'
import { useMutation } from '@tanstack/react-query'

import { setPlanWeekItem } from '../api/planClient.js'
import { usePlanWeek, useOptimisticWeek, usePlantWeek } from './usePlanQuery.js'

// Pack sizes chosen for one week only, and demands shaved to fit a pack. Kept
// beside the week rather than written to the mapping, which is the whole point
// of the distinction: buying the big bag once is not the same decision as always
// buying it, and it should cost nothing and expire on its own. The standing
// version of the same choice is a pack *preference*, which belongs to the
// account — see setPackPreference in api/client.js.
//
// Server-side since accounts, so a pack chosen on the laptop is the pack the
// phone shows; the hook's shape is unchanged.

export function useWeekPackChoices(weekStart) {
  const week = usePlanWeek(weekStart)
  const plantWeek = usePlantWeek()
  const optimistic = useOptimisticWeek()
  const save = useMutation({ mutationFn: setPlanWeekItem, onSuccess: plantWeek })

  const setWeekPack = useCallback(
    (ingredientKey, sku) => {
      if (!weekStart) return
      optimistic(weekStart, (current) => {
        const packOverrides = { ...current.pack_overrides }
        if (sku) packOverrides[ingredientKey] = sku
        else delete packOverrides[ingredientKey]
        return { ...current, pack_overrides: packOverrides }
      }).then((rollback) =>
        save.mutateAsync({ weekStart, ingredientKey, packSku: sku ?? null }).catch((error) => {
          rollback()
          throw error
        }),
      )
    },
    [optimistic, save, weekStart],
  )

  const setWeekSnap = useCallback(
    (ingredientKey, enabled) => {
      if (!weekStart) return
      optimistic(weekStart, (current) => {
        const snapOverrides = { ...current.snap_overrides }
        if (enabled) snapOverrides[ingredientKey] = true
        else delete snapOverrides[ingredientKey]
        return { ...current, snap_overrides: snapOverrides }
      }).then((rollback) =>
        save.mutateAsync({ weekStart, ingredientKey, snapped: Boolean(enabled) }).catch((error) => {
          rollback()
          throw error
        }),
      )
    },
    [optimistic, save, weekStart],
  )

  const setWeekPackAndSnap = useCallback(
    (ingredientKey, sku, snapped) => {
      if (!weekStart) return
      optimistic(weekStart, (current) => {
        const packOverrides = { ...current.pack_overrides }
        const snapOverrides = { ...current.snap_overrides }
        if (sku) packOverrides[ingredientKey] = sku
        else delete packOverrides[ingredientKey]
        if (snapped) snapOverrides[ingredientKey] = true
        else delete snapOverrides[ingredientKey]
        return { ...current, pack_overrides: packOverrides, snap_overrides: snapOverrides }
      }).then((rollback) =>
        save.mutateAsync({ weekStart, ingredientKey, packSku: sku ?? null, snapped: Boolean(snapped) }).catch((error) => {
          rollback()
          throw error
        }),
      )
    },
    [optimistic, save, weekStart],
  )

  return {
    packOverrides: week.packOverrides,
    setWeekPack,
    snapOverrides: week.snapOverrides,
    setWeekSnap,
    setWeekPackAndSnap,
  }
}
