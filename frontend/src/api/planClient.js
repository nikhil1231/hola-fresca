// The plan: which recipes are in which week, and the per-week decisions made
// about the basket they add up to. This lived in localStorage until accounts
// arrived, which is why every function here has a localStorage-shaped signature
// — the hooks that call them kept their old API so the pages did not have to
// change.
//
// Every write touches one row. There is no "save the plan" call, deliberately:
// two devices with the same week open would otherwise take turns overwriting
// each other with whatever each of them last read.

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

const weekPath = (weekStart) => `/api/plan/weeks/${encodeURIComponent(weekStart)}`

export function fetchPlan() {
  return request('/api/plan')
}

export function addPlanRecipe({ weekStart, recipeId, portions, protein }) {
  return request(`${weekPath(weekStart)}/recipes`, {
    method: 'POST',
    body: { recipe_id: recipeId, portions, protein },
  })
}

/** Only the fields passed are changed. `protein: null` clears the modifier,
 *  which is why it is spelled out rather than dropped when absent. */
export function updatePlanRecipe({ weekStart, recipeId, portions, protein }) {
  const body = {}
  if (portions !== undefined) body.portions = portions
  if (protein !== undefined) body.protein = protein
  return request(`${weekPath(weekStart)}/recipes/${recipeId}`, { method: 'PATCH', body })
}

export function removePlanRecipe({ weekStart, recipeId }) {
  return request(`${weekPath(weekStart)}/recipes/${recipeId}`, { method: 'DELETE' })
}

/** One per-week decision about one basket line: which pack, snapped, or owned.
 *  Fields left undefined are not touched. */
export function setPlanWeekItem({ weekStart, ingredientKey, packSku, snapped, owned }) {
  const body = {}
  if (packSku !== undefined) body.pack_sku = packSku
  if (snapped !== undefined) body.snapped = snapped
  if (owned !== undefined) body.owned = owned
  return request(`${weekPath(weekStart)}/items/${encodeURIComponent(ingredientKey)}`, {
    method: 'PUT',
    body,
  })
}

export function importPlan(weeks) {
  return request('/api/plan/import', { method: 'POST', body: { weeks } })
}
