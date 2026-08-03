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

function accountQuery(accountId) {
  const params = new URLSearchParams()
  if (accountId) params.set('account_id', accountId)
  const query = params.toString()
  return query ? `?${query}` : ''
}

export function fetchOcadoAccounts() {
  return getJSON('/api/ocado/accounts')
}

export function fetchOcadoStatus(accountId) {
  return getJSON(`/api/ocado/status${accountQuery(accountId)}`)
}

export function startOcadoLogin(accountId) {
  return postJSON('/api/ocado/login', { account_id: accountId })
}

export function refreshOcadoSession(accountId) {
  return postJSON('/api/ocado/session/refresh', { account_id: accountId })
}

export function submitOcadoOtp({ accountId, code }) {
  return postJSON('/api/ocado/otp', { account_id: accountId, code })
}

export function pushOcadoBasket({
  accountId,
  selections,
  ownedItemKeys = [],
  packOverrides = {},
  weekStart = null,
}) {
  return postJSON('/api/ocado/basket/push', {
    account_id: accountId,
    selections,
    owned_item_keys: ownedItemKeys,
    pack_overrides: packOverrides,
    week_start: weekStart,
  })
}

// What a push would change, without changing it. The cart is shared with the
// rest of your shopping and a sync can now remove things, so this is what makes
// the button safe to press: it names what of yours gets left alone.
export function planOcadoBasket({ accountId, selections, ownedItemKeys = [], packOverrides = {} }) {
  return postJSON('/api/ocado/basket/plan', {
    account_id: accountId,
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

export function fetchOcadoBasket(accountId) {
  return getJSON(`/api/ocado/basket${accountQuery(accountId)}`)
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
