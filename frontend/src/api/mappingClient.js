// Fetch wrappers for the ingredient->product mapping review API.

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

async function getJSON(path) {
  const res = await fetch(path)
  if (!res.ok) throw await responseError(res)
  return res.json()
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

export function fetchMappingList(status, { page = 1, pageSize = 100, q = '' } = {}) {
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  if (q) params.set('q', q)
  params.set('page', String(page))
  params.set('page_size', String(pageSize))
  return getJSON(`/api/mapping/ingredients?${params.toString()}`)
}

export function fetchMappingDetail(key) {
  return getJSON(`/api/mapping/ingredients/${encodeURIComponent(key)}`)
}

export function saveMappingDecision(key, body) {
  return postJSON(`/api/mapping/ingredients/${encodeURIComponent(key)}`, body)
}

// Live Ocado re-search: widens this ingredient's candidate pool. Slow (drives a
// real browser), so the UI shows a loading state.
export function searchMappingCandidates(key, term) {
  return postJSON(`/api/mapping/ingredients/${encodeURIComponent(key)}/search`, { term })
}

export function fetchMappingStats() {
  return getJSON('/api/mapping/stats')
}

export function fetchAliases() {
  return getJSON('/api/mapping/aliases')
}

export function fetchAliasOptions({ exclude, q = '', limit = 200 } = {}) {
  const params = new URLSearchParams()
  if (exclude) params.set('exclude', exclude)
  if (q) params.set('q', q)
  params.set('limit', String(limit))
  return getJSON(`/api/mapping/alias-options?${params.toString()}`)
}

// Pass null to clear the alias and return the ingredient to the review queue.
export function setMappingAlias(key, aliasOf) {
  return postJSON(`/api/mapping/ingredients/${encodeURIComponent(key)}/alias`, {
    alias_of: aliasOf,
  })
}

// Kicks off a background job; poll fetchJob() until it is no longer 'running'.
export function startGenerate(count = 10) {
  return postJSON('/api/mapping/generate', { count })
}

export function fetchJob(jobId) {
  return getJSON(`/api/mapping/jobs/${encodeURIComponent(jobId)}`)
}

export function bulkApprove(keys) {
  return postJSON('/api/mapping/bulk-approve', { keys })
}

// --- Manually sourced products (things Ocado does not sell) -------------------

export function fetchManualProducts() {
  return getJSON('/api/mapping/manual-products')
}

// Keyed on name: posting an existing name updates that product in place.
export function saveManualProduct(body) {
  return postJSON('/api/mapping/manual-products', body)
}

export async function deleteManualProduct(sku) {
  const res = await fetch(`/api/mapping/manual-products/${encodeURIComponent(sku)}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw await responseError(res)
  return res.json()
}

// Creates the product and approves it as this ingredient's mapping in one step.
export function resolveWithManualProduct(key, body) {
  return postJSON(`/api/mapping/ingredients/${encodeURIComponent(key)}/manual`, body)
}

// Offers an existing manual product as a candidate for another ingredient.
export function attachManualProduct(key, sku) {
  return postJSON(
    `/api/mapping/ingredients/${encodeURIComponent(key)}/manual/${encodeURIComponent(sku)}`,
    {},
  )
}

// --- Specialist catalogue (Seasoned Pioneers) --------------------------------

export function fetchCatalogueStatus() {
  return getJSON('/api/mapping/catalogue/status')
}

// Adds the catalogue's best matches to this ingredient's candidate pool. Pure
// string matching over cached products, so it returns immediately.
export function attachCatalogueMatches(key, { q, minScore, limit } = {}) {
  const params = new URLSearchParams()
  if (q) params.set('q', q)
  if (minScore != null) params.set('min_score', String(minScore))
  if (limit != null) params.set('limit', String(limit))
  const query = params.toString()
  return postJSON(
    `/api/mapping/ingredients/${encodeURIComponent(key)}/catalogue${query ? `?${query}` : ''}`,
    {},
  )
}

export function attachCatalogueAcrossQueue(body = {}) {
  return postJSON('/api/mapping/catalogue/attach', body)
}
