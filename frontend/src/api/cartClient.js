// Session and basket-push calls, for whichever shop has a cart.
//
// These were `/api/ocado/*` and this file was ocadoClient.js. The retailer is
// now a path segment rather than something baked into the URL, because it
// decides *which trolley gets written to* — see app/api/cart.py. Delivery slots
// stayed behind in ocadoClient.js: only Ocado has them.
//
// Nothing here sends an account id. The server resolves the caller's own account
// from their identity, so there is no longer a "which account" for the client to
// get wrong, remember in localStorage, or be handed by somebody else's browser.

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

// Every call here needs one, and a missing retailer would otherwise land on a
// URL like /api/cart/undefined/status and come back a puzzling 404.
function base(retailer) {
  if (!retailer) throw new Error('cartClient: no retailer given')
  return `/api/cart/${encodeURIComponent(retailer)}`
}

export function fetchCartStatus(retailer) {
  return getJSON(`${base(retailer)}/status`)
}

export function startCartLogin({ retailer, email, password }) {
  return postJSON(`${base(retailer)}/login`, { email, password })
}

export function refreshCartSession(retailer) {
  return postJSON(`${base(retailer)}/session/refresh`)
}

export function submitCartOtp({ retailer, code }) {
  return postJSON(`${base(retailer)}/otp`, { code })
}

export function logoutCart(retailer) {
  return postJSON(`${base(retailer)}/logout`)
}

export function pushCartBasket({
  retailer,
  selections,
  ownedItemKeys = [],
  packOverrides = {},
  snapOverrides = {},
  weekStart = null,
}) {
  return postJSON(`${base(retailer)}/basket/push`, {
    selections,
    owned_item_keys: ownedItemKeys,
    pack_overrides: packOverrides,
    snap_overrides: snapOverrides,
    week_start: weekStart,
  })
}

// What a push would change, without changing it. The cart is shared with the
// rest of your shopping and a sync can now remove things, so this is what makes
// the button safe to press: it names what of yours gets left alone.
export function planCartBasket({
  retailer,
  selections,
  ownedItemKeys = [],
  packOverrides = {},
  snapOverrides = {},
}) {
  return postJSON(`${base(retailer)}/basket/plan`, {
    selections,
    owned_item_keys: ownedItemKeys,
    pack_overrides: packOverrides,
    snap_overrides: snapOverrides,
  })
}

export function fetchCartBasket(retailer) {
  return getJSON(`${base(retailer)}/basket`)
}
