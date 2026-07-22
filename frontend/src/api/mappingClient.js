// Fetch wrappers for the ingredient→product mapping review API.

async function getJSON(path) {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

async function postJSON(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export function fetchMappingList(status) {
  const q = status ? `?status=${encodeURIComponent(status)}` : ''
  return getJSON(`/api/mapping/ingredients${q}`)
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

export function bulkApprove(keys) {
  return postJSON('/api/mapping/bulk-approve', { keys })
}
