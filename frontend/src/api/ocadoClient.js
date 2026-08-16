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

// Slots belong to whichever Ocado account the server resolves for the caller —
// they are booked against that account's address and paid for with its card —
// so, as with the cart calls, no account is named here.
export function fetchOcadoSlots({ ddid, region } = {}) {
  const params = new URLSearchParams()
  if (ddid) params.set('ddid', ddid)
  if (region) params.set('region', region)
  const suffix = params.toString() ? `?${params.toString()}` : ''
  return getJSON(`/api/ocado/slots${suffix}`)
}

export function reserveOcadoSlot({ slotId, ddid, region }) {
  return postJSON('/api/ocado/slots/reserve', {
    slot_id: slotId,
    ddid: ddid || null,
    region: region || null,
  })
}
