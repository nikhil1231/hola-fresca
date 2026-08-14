import { useCallback, useMemo } from 'react'
import { useMutation } from '@tanstack/react-query'

import { setPlanWeekItem } from '../api/planClient.js'
import { usePlanWeek, useOptimisticWeek, usePlantWeek } from './usePlanQuery.js'

// "I already have this" — the basket lines to leave out of the shop, for one
// week. Server-side since accounts, so ticking something off on the phone in the
// kitchen is what the laptop pushes to the retailer.

export function useOwnedBasketItems(weekStart) {
  const week = usePlanWeek(weekStart)
  const plantWeek = usePlantWeek()
  const optimistic = useOptimisticWeek()
  const save = useMutation({ mutationFn: setPlanWeekItem, onSuccess: plantWeek })

  const ownedItemKeys = week.ownedItemKeys
  const ownedItemKeySet = useMemo(() => new Set(ownedItemKeys), [ownedItemKeys])

  const setItemOwned = useCallback(
    (key, owned) => {
      if (!weekStart) return
      optimistic(weekStart, (current) => {
        const keys = new Set(current.owned_item_keys ?? [])
        if (owned) keys.add(key)
        else keys.delete(key)
        return { ...current, owned_item_keys: [...keys].sort() }
      }).then((rollback) =>
        save.mutateAsync({ weekStart, ingredientKey: key, owned: Boolean(owned) }).catch((error) => {
          rollback()
          throw error
        }),
      )
    },
    [optimistic, save, weekStart],
  )

  return { ownedItemKeys, ownedItemKeySet, setItemOwned }
}
