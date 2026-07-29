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

export function submitOcadoOtp(code) {
  return postJSON('/api/ocado/otp', { code })
}

export function pushOcadoBasket(selections) {
  return postJSON('/api/ocado/basket/push', { selections })
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

