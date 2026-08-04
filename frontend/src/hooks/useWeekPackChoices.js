import { useCallback, useEffect, useMemo, useState } from 'react'

const STORAGE_KEY = 'hola-fresca.week-pack-choices.v1'

// Pack sizes chosen for one week only. Kept beside the week rather than written
// to the mapping, which is the whole point of the distinction: buying the big
// bag once is not the same decision as always buying it, and it should cost
// nothing and expire on its own.
function normalize(value) {
  if (!value || typeof value !== 'object') return { weeks: {}, snaps: {} }
  const weeks = {}
  const snaps = {}
  for (const [weekStart, choices] of Object.entries(value.weeks ?? {})) {
    if (!choices || typeof choices !== 'object') continue
    const clean = {}
    for (const [key, sku] of Object.entries(choices)) {
      if (typeof key === 'string' && typeof sku === 'string' && key && sku) clean[key] = sku
    }
    if (Object.keys(clean).length) weeks[weekStart] = clean
  }
  for (const [weekStart, choices] of Object.entries(value.snaps ?? {})) {
    if (!choices || typeof choices !== 'object') continue
    const clean = Object.fromEntries(Object.entries(choices).filter(([key, enabled]) => key && enabled === true))
    if (Object.keys(clean).length) snaps[weekStart] = clean
  }
  return { weeks, snaps }
}

function read() {
  if (typeof window === 'undefined') return { weeks: {}, snaps: {} }
  try {
    return normalize(JSON.parse(window.localStorage.getItem(STORAGE_KEY)))
  } catch {
    return { weeks: {}, snaps: {} }
  }
}

export function useWeekPackChoices(weekStart) {
  const [state, setState] = useState(read)

  useEffect(() => {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
    }
  }, [state])

  useEffect(() => {
    function handleStorage(event) {
      if (event.key === STORAGE_KEY) setState(read())
    }
    window.addEventListener('storage', handleStorage)
    return () => window.removeEventListener('storage', handleStorage)
  }, [])

  const packOverrides = useMemo(() => state.weeks[weekStart] ?? {}, [state.weeks, weekStart])
  const snapOverrides = useMemo(() => state.snaps[weekStart] ?? {}, [state.snaps, weekStart])

  const setWeekPack = useCallback(
    (ingredientKey, sku) => {
      setState((current) => {
        const week = { ...(current.weeks[weekStart] ?? {}) }
        if (sku) week[ingredientKey] = sku
        else delete week[ingredientKey]
        return normalize({ ...current, weeks: { ...current.weeks, [weekStart]: week } })
      })
    },
    [weekStart],
  )

  const setWeekSnap = useCallback((ingredientKey, enabled) => {
    setState((current) => {
      const week = { ...(current.snaps[weekStart] ?? {}) }
      if (enabled) week[ingredientKey] = true
      else delete week[ingredientKey]
      return normalize({ ...current, snaps: { ...current.snaps, [weekStart]: week } })
    })
  }, [weekStart])

  const setWeekPackAndSnap = useCallback((ingredientKey, sku, snapped) => {
    setState((current) => {
      const packs = { ...(current.weeks[weekStart] ?? {}) }
      const snaps = { ...(current.snaps[weekStart] ?? {}) }
      if (sku) packs[ingredientKey] = sku
      else delete packs[ingredientKey]
      if (snapped) snaps[ingredientKey] = true
      else delete snaps[ingredientKey]
      return normalize({
        ...current,
        weeks: { ...current.weeks, [weekStart]: packs },
        snaps: { ...current.snaps, [weekStart]: snaps },
      })
    })
  }, [weekStart])

  return { packOverrides, setWeekPack, snapOverrides, setWeekSnap, setWeekPackAndSnap }
}
