// Thin fetch wrappers around the recipe API. All requests go through Vite's
// /api proxy to the FastAPI backend.

async function getJSON(path) {
  const res = await fetch(path)
  if (res.ok) return res.json()

  let detail = null
  try {
    const body = await res.json()
    detail = body?.detail
  } catch {
    // Keep the status-only fallback when the server did not return JSON.
  }
  throw new Error(detail ? `HTTP ${res.status}: ${detail}` : `HTTP ${res.status}`)
}

// Filter params that are arrays (repeatable query params) vs scalars.
const ARRAY_KEYS = new Set(['cuisine', 'diet', 'tag', 'protein', 'exclude'])

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

export function fetchRecipes(filters, page, pageSize = 24) {
  const params = buildRecipeParams(filters)
  params.set('page', String(page))
  params.set('page_size', String(pageSize))
  return getJSON(`/api/recipes?${params.toString()}`)
}

export function fetchRecipe(id) {
  return getJSON(`/api/recipes/${id}`)
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
