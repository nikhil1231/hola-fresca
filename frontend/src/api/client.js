// Thin fetch wrappers around the recipe API. All requests go through Vite's
// /api proxy to the FastAPI backend.

async function getJSON(path) {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
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

export function fetchFacets() {
  return getJSON('/api/facets')
}
