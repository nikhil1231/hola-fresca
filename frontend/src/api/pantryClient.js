// The cupboard: what past shops left behind, decayed toward the next one.
// Reads come back per-ingredient with provenance (which shop, how many cycles
// held). Writes are all one shape — a person overruling the model — and differ
// only in how much they say: it has gone, it is still there, or there is
// exactly this much of it.

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

async function request(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    ...(body === undefined
      ? {}
      : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })
  if (!res.ok) throw await responseError(res)
  return res.json()
}

export function fetchPantry() {
  return request('/api/pantry')
}

// Candidates for the add box: approved, non-staple ingredients at the active
// shop, each carrying how well it keeps so the page can warn about the chiller.
export function fetchPantryIngredients(q = '') {
  return request(`/api/pantry/ingredients?q=${encodeURIComponent(q)}`)
}

/** One statement about one shelf. Only the fields passed are acted on:
 *  `present` is the quick correction, a quantity is the fuller one and also
 *  how something new is added. The key travels in the body because ingredient
 *  keys hold slashes and a path segment would eat them. */
export function setPantryItem({ ingredientKey, present, grams, qty }) {
  const body = { ingredient_key: ingredientKey }
  if (present !== undefined) body.present = present
  if (grams !== undefined) body.grams = grams
  if (qty !== undefined) body.qty = qty
  return request('/api/pantry/item', { method: 'PUT', body })
}

export function removePantryItem(ingredientKey) {
  return request(`/api/pantry/item?ingredient_key=${encodeURIComponent(ingredientKey)}`, {
    method: 'DELETE',
  })
}
