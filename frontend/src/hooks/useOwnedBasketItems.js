import { useCallback, useEffect, useMemo, useState } from 'react'

const STORAGE_KEY = 'hola-fresca.owned-basket-items.v1'

function normalizeOwnedItems(value) {
  if (!value || typeof value !== 'object') return { weeks: {} }
  const weeks = {}
  for (const [weekStart, keys] of Object.entries(value.weeks ?? {})) {
    if (!Array.isArray(keys)) continue
    weeks[weekStart] = [...new Set(keys.filter((key) => typeof key === 'string' && key))]
  }
  return { weeks }
}

function readStoredOwnedItems() {
  if (typeof window === 'undefined') return { weeks: {} }
  try {
    return normalizeOwnedItems(JSON.parse(window.localStorage.getItem(STORAGE_KEY)))
  } catch {
    return { weeks: {} }
  }
}

function writeStoredOwnedItems(items) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items))
}

export function useOwnedBasketItems(weekStart) {
  const [items, setItems] = useState(readStoredOwnedItems)

  useEffect(() => {
    writeStoredOwnedItems(items)
  }, [items])

  useEffect(() => {
    function handleStorage(event) {
      if (event.key === STORAGE_KEY) setItems(readStoredOwnedItems())
    }
    window.addEventListener('storage', handleStorage)
    return () => window.removeEventListener('storage', handleStorage)
  }, [])

  const ownedItemKeys = useMemo(() => items.weeks[weekStart] ?? [], [items.weeks, weekStart])
  const ownedItemKeySet = useMemo(() => new Set(ownedItemKeys), [ownedItemKeys])

  const setItemOwned = useCallback(
    (key, owned) => {
      setItems((current) => {
        const weekKeys = new Set(current.weeks[weekStart] ?? [])
        if (owned) weekKeys.add(key)
        else weekKeys.delete(key)
        return normalizeOwnedItems({
          ...current,
          weeks: {
            ...current.weeks,
            [weekStart]: [...weekKeys],
          },
        })
      })
    },
    [weekStart],
  )

  return { ownedItemKeys, ownedItemKeySet, setItemOwned }
}
