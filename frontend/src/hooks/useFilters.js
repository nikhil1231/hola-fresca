import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

// Keys stored as repeatable (multi-value) params.
const ARRAY_KEYS = ['cuisine', 'diet', 'tag', 'protein', 'exclude']
// Keys stored as single numeric params.
const NUMBER_KEYS = ['max_time', 'min_protein', 'min_protein_ratio', 'max_kcal', 'difficulty']

export const DEFAULT_SORT = 'popular'

// Parse the URL search params into a plain filters object used by the API layer.
export function parseFilters(searchParams) {
  const filters = {}
  const q = searchParams.get('q')
  if (q) filters.q = q
  const sort = searchParams.get('sort')
  if (sort) filters.sort = sort
  for (const key of ARRAY_KEYS) {
    const values = searchParams.getAll(key)
    if (values.length) filters[key] = values
  }
  for (const key of NUMBER_KEYS) {
    const value = searchParams.get(key)
    if (value != null && value !== '') filters[key] = Number(value)
  }
  return filters
}

// Count of active filters (everything except sort + free-text search), for the
// mobile "Filters" button badge.
export function countActiveFilters(filters) {
  let n = 0
  for (const key of ARRAY_KEYS) n += filters[key]?.length ?? 0
  for (const key of NUMBER_KEYS) if (filters[key] != null) n += 1
  return n
}

// URL-synced filter state. Returns the parsed filters plus mutators that write
// back to the query string (so every view is shareable/bookmarkable).
export function useFilters() {
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => parseFilters(searchParams), [searchParams])

  const setScalar = useCallback(
    (key, value) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        if (value == null || value === '' || value === DEFAULT_SORT) next.delete(key)
        else next.set(key, String(value))
        return next
      })
    },
    [setSearchParams],
  )

  const toggleArrayValue = useCallback(
    (key, value) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        const current = next.getAll(key)
        next.delete(key)
        const updated = current.includes(value)
          ? current.filter((v) => v !== value)
          : [...current, value]
        for (const v of updated) next.append(key, v)
        return next
      })
    },
    [setSearchParams],
  )

  const setArray = useCallback(
    (key, values) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        next.delete(key)
        for (const v of values ?? []) next.append(key, v)
        return next
      })
    },
    [setSearchParams],
  )

  const clearAll = useCallback(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams()
      // Preserve sort + search; clear only the facet filters.
      const q = prev.get('q')
      const sort = prev.get('sort')
      if (q) next.set('q', q)
      if (sort) next.set('sort', sort)
      return next
    })
  }, [setSearchParams])

  return { filters, setScalar, setArray, toggleArrayValue, clearAll }
}
