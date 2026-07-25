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
