import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

// Keys stored as repeatable (multi-value) params.
const ARRAY_KEYS = ['cuisine', 'diet', 'tag', 'protein', 'exclude', 'course']
// Keys stored as single numeric params.
const NUMBER_KEYS = ['max_time', 'min_protein', 'min_protein_ratio', 'max_kcal', 'difficulty']
const BOOLEAN_KEYS = ['rated', 'wishlisted']

export const DEFAULT_SORT = 'best_fit'
const DEFAULT_EXCLUDES = ['unmapped']
const SHOW_UNMAPPED_KEY = 'show_unmapped'
// An absent course param means mains, both here and in the API. Spelling that
// out in the parsed filters is what lets the chips show Mains as selected and
// still toggle against the same list the user is looking at.
const DEFAULT_COURSES = ['main']
// Keys whose parsed value has defaults folded in, so a toggle has to read the
// parsed list rather than the raw query string.
const DEFAULTED_KEYS = new Set(['exclude', 'course'])

// Parse the URL search params into a plain filters object used by the API layer.
export function parseFilters(searchParams) {
  const filters = {}
  const q = searchParams.get('q')
  if (q) filters.q = q
  const sort = searchParams.get('sort')
  if (sort) filters.sort = sort
  for (const key of ARRAY_KEYS) {
    const values = searchParams.getAll(key)
    if (key === 'exclude' && searchParams.get(SHOW_UNMAPPED_KEY) !== '1') {
      if (!values.includes('unmapped')) values.unshift('unmapped')
    }
    if (key === 'course' && values.length === 0) values.push(...DEFAULT_COURSES)
    if (values.length) filters[key] = values
  }
  for (const key of NUMBER_KEYS) {
    const value = searchParams.get(key)
    if (value != null && value !== '') filters[key] = Number(value)
  }
  for (const key of BOOLEAN_KEYS) {
    if (searchParams.get(key) === 'true') filters[key] = true
  }
  return filters
}

// Count of active filters (everything except sort + free-text search), for the
// mobile "Filters" button badge.
export function countActiveFilters(filters) {
  let n = 0
  for (const key of ARRAY_KEYS) {
    const values = filters[key] ?? []
    // The defaults are not choices the user made, so they do not light up the
    // "Clear all" badge on an untouched page.
    const defaults =
      key === 'exclude' ? DEFAULT_EXCLUDES : key === 'course' ? DEFAULT_COURSES : []
    n += defaults.length
      ? values.filter((value) => !defaults.includes(value)).length
      : values.length
  }
  for (const key of NUMBER_KEYS) if (filters[key] != null) n += 1
  for (const key of BOOLEAN_KEYS) if (filters[key]) n += 1
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
        const current = DEFAULTED_KEYS.has(key)
          ? parseFilters(next)[key] ?? []
          : next.getAll(key)
        next.delete(key)
        let updated = current.includes(value)
          ? current.filter((v) => v !== value)
          : [...current, value]
        // Turning every course off would show mains anyway, since that is what
        // an empty list means to the API — so the chips would disagree with the
        // results. Snap back to the default instead.
        if (key === 'course' && updated.length === 0) updated = DEFAULT_COURSES
        for (const v of updated) next.append(key, v)
        if (key === 'exclude') {
          if (updated.includes('unmapped')) next.delete(SHOW_UNMAPPED_KEY)
          else next.set(SHOW_UNMAPPED_KEY, '1')
        }
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
        if (key === 'exclude') {
          if ((values ?? []).includes('unmapped')) next.delete(SHOW_UNMAPPED_KEY)
          else next.set(SHOW_UNMAPPED_KEY, '1')
        }
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
