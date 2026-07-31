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

export function fetchOcadoStatus() {
  return getJSON('/api/ocado/status')
}

export function startOcadoLogin() {
  return postJSON('/api/ocado/login')
}

export function refreshOcadoSession() {
  return postJSON('/api/ocado/session/refresh')
}

export function submitOcadoOtp(code) {
  return postJSON('/api/ocado/otp', { code })
}

export function pushOcadoBasket({ selections, ownedItemKeys = [], packOverrides = {}, weekStart = null }) {
  return postJSON('/api/ocado/basket/push', {
    selections,
    owned_item_keys: ownedItemKeys,
    pack_overrides: packOverrides,
    week_start: weekStart,
  })
}

// What a push would change, without changing it. The cart is shared with the
// rest of your shopping and a sync can now remove things, so this is what makes
// the button safe to press: it names what of yours gets left alone.
export function planOcadoBasket({ selections, ownedItemKeys = [], packOverrides = {} }) {
  return postJSON('/api/ocado/basket/plan', {
    selections,
    owned_item_keys: ownedItemKeys,
    pack_overrides: packOverrides,
  })
}

// Re-reads stock and price for every product this basket could be covered from.
// Needs no Ocado login, so the basket page can offer it unconditionally.
export function refreshOcadoStock({ selections, packOverrides = {} }) {
  return postJSON('/api/ocado/stock/refresh', { selections, pack_overrides: packOverrides })
}

export function fetchOcadoBasket() {
  return getJSON('/api/ocado/basket')
}

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
