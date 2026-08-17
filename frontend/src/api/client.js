// Thin fetch wrappers around the recipe API. All requests go through Vite's
// /api proxy to the FastAPI backend.

async function responseError(res) {
  let detail = null
  try {
    const body = await res.json()
    detail = body?.detail
  } catch {
    // Keep the status-only fallback when the server did not return JSON.
  }
  return new Error(detail ? `HTTP ${res.status}: ${detail}` : `HTTP ${res.status}`)
}

async function getJSON(path) {
  const res = await fetch(path)
  if (res.ok) return res.json()

  throw await responseError(res)
}

async function postJSON(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw await responseError(res)
  return res.json()
}

async function putJSON(path, body) {
  const res = await fetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw await responseError(res)
  return res.json()
}

async function deleteJSON(path) {
  const res = await fetch(path, { method: 'DELETE' })
  if (!res.ok) throw await responseError(res)
  return res.json()
}

// Filter params that are arrays (repeatable query params) vs scalars. Anything
// missing here is stringified instead, which for an array means "main,side" —
// one value the API has never heard of, so the filter silently does nothing.
// Keep in sync with ARRAY_KEYS in hooks/useFilters.js.
const ARRAY_KEYS = new Set(['cuisine', 'diet', 'tag', 'protein', 'exclude', 'course'])

// Build a URLSearchParams from a plain filter object, expanding arrays into
// repeated keys and dropping empty values.
export function buildRecipeParams(filters = {}) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value == null || value === '') continue
    if (ARRAY_KEYS.has(key)) {
      for (const v of value) if (v) params.append(key, v)
    } else {
      params.set(key, String(value))
    }
  }
  return params
}

export function fetchRecipes(filters, page, pageSize = 24, { offset, excludeIds = [] } = {}) {
  const params = buildRecipeParams(filters)
  params.set('page', String(page))
  params.set('page_size', String(pageSize))
  if (offset != null) params.set('offset', String(offset))
  for (const id of excludeIds) params.append('exclude_id', String(id))
  return getJSON(`/api/recipes?${params.toString()}`)
}

// week_start is what lets the backend spend the pantry: without a week there is
// no "before this shop" to read the cupboard at, so it prices from scratch.
export function fetchPlannerBasket(selections, packOverrides = {}, snapOverrides = {}, weekStart = null) {
  return postJSON('/api/planner/basket', {
    selections,
    pack_overrides: packOverrides,
    snap_overrides: snapOverrides,
    week_start: weekStart ?? undefined,
  })
}

// Re-reads stock and price at the active shop for every product this basket
// could be covered from. Needs no login anywhere, so the basket page can offer
// it to whoever is looking at it.
export function refreshPlannerStock({ selections, packOverrides = {}, snapOverrides = {} }) {
  return postJSON('/api/planner/stock/refresh', {
    selections,
    pack_overrides: packOverrides,
    snap_overrides: snapOverrides,
  })
}

export function fetchPlannerSuggestions({
  selections,
  filters = {},
  candidatePortions = 4,
  page = 1,
  pageSize = 24,
  offset = null,
}) {
  return postJSON('/api/planner/suggestions', {
    selections,
    filters,
    candidate_portions: candidatePortions,
    page,
    page_size: pageSize,
    offset,
  })
}

export function fetchRecipe(id) {
  return getJSON(`/api/recipes/${id}`)
}

export function startCookMap(id) {
  return postJSON(`/api/recipes/${id}/cook-map`, {})
}

export function fetchCookMap(id, modifier) {
  const params = new URLSearchParams()
  for (const key of ['swap_to', 'scale', 'target_mode', 'target_value']) {
    const value = modifier?.[key]
    if (value != null && value !== '') params.set(key, String(value))
  }
  const query = params.size ? `?${params.toString()}` : ''
  return getJSON(`/api/recipes/${id}/cook-map${query}`)
}

export function retryCookMap(id) {
  return postJSON(`/api/recipes/${id}/cook-map/retry`, {})
}

// The recipe as it would be with a protein swap/scale applied. A POST that
// writes nothing: the body describes a hypothetical, and the recipe is untouched.
export function fetchProteinPreview(id, modifier) {
  return postJSON(`/api/recipes/${id}/protein/preview`, modifier ?? {})
}

export function setPersonalRecipeRating(id, rating) {
  return putJSON(`/api/recipes/${id}/personal-rating`, { rating })
}

export function setRecipeWishlist(id, wishlisted) {
  return putJSON(`/api/recipes/${id}/wishlist`, { wishlisted })
}

// A standing "always buy this size" choice for one ingredient; null hands the
// size back to the planner.
export function setPackPreference({ ingredientKey, sku }) {
  return putJSON('/api/planner/preferences/pack', { ingredient_key: ingredientKey, sku })
}

export function hideRecipe(id) {
  return postJSON(`/api/recipes/${id}/hide`, {})
}

// Admits a recipe the curation rules cut into the shared library, or withdraws
// one previously admitted. Admin-only, and not the same act as a wishlist: this
// changes what everybody can search, price and plan.
export function setRecipeLibrary(id, inLibrary) {
  return inLibrary
    ? postJSON(`/api/recipes/${id}/library`, {})
    : deleteJSON(`/api/recipes/${id}/library`)
}

// Flags the macros as suspicious and starts a background audit; poll the
// returned job_id with fetchAuditJob until it is no longer 'running'.
export async function flagRecipe(id) {
  const res = await fetch(`/api/recipes/${id}/flag`, { method: 'POST' })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export function fetchAuditJob(jobId) {
  return getJSON(`/api/recipes/audit-jobs/${encodeURIComponent(jobId)}`)
}

// Puts the source's original numbers back.
export async function revertRecipeEdits(id) {
  const res = await fetch(`/api/recipes/${id}/revert`, { method: 'POST' })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export function fetchFacets() {
  return getJSON('/api/facets')
}
