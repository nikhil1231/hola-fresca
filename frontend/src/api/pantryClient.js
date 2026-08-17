// The cupboard: what past shops left behind, decayed toward the next one.
// Reads come back per-ingredient with provenance (which shop, how many cycles
// held); the only writes are the two corrections a person can make without
// weighing anything — ran out, or still there.

async function responseError(res) {
  let detail = null
  try {
    const body = await res.json()
    detail = body?.detail
  } catch {
    // Status-only fallback below.
  }
  return new Error(detail ? `HTTP ${res.status}: ${detail}` : `HTTP ${res.status}`)
}

export async function fetchPantry() {
  const res = await fetch('/api/pantry')
  if (!res.ok) throw await responseError(res)
  return res.json()
}

// The key travels in the body rather than the path: ingredient keys hold
// slashes, and a path segment would eat them.
export async function setPantryItem({ ingredientKey, present }) {
  const res = await fetch('/api/pantry/item', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ingredient_key: ingredientKey, present }),
  })
  if (!res.ok) throw await responseError(res)
  return res.json()
}
