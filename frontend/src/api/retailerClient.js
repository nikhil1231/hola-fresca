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

export async function fetchRetailers() {
  const res = await fetch('/api/retailers')
  if (!res.ok) throw await responseError(res)
  return res.json()
}

export async function setActiveRetailer(retailer) {
  const res = await fetch('/api/retailers', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ retailer }),
  })
  if (!res.ok) throw await responseError(res)
  return res.json()
}
