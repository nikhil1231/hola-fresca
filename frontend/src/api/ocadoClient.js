// Delivery slots — the only part of the shop integration that is still Ocado's
// alone. Everything a shop with a cart has in common (sessions, login, OTP, the
// basket plan and push) lives in cartClient.js and takes a retailer.

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

async function postJSON(path, body = {}) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw await responseError(res)
  return res.json()
}

export function fetchOcadoSlots({ accountId, ddid, region } = {}) {
  const params = new URLSearchParams()
  if (accountId) params.set('account_id', accountId)
  if (ddid) params.set('ddid', ddid)
  if (region) params.set('region', region)
  const suffix = params.toString() ? `?${params.toString()}` : ''
  return getJSON(`/api/ocado/slots${suffix}`)
}

export function reserveOcadoSlot({ accountId, slotId, ddid, region }) {
  return postJSON('/api/ocado/slots/reserve', {
    account_id: accountId,
    slot_id: slotId,
    ddid: ddid || null,
    region: region || null,
  })
}
