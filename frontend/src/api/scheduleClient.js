// The shopping schedule: cadence, cutoff, skipped weeks and the pause switch.
// Everything here returns the whole schedule, because every one of these writes
// can change which week is the active one.

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

async function putJSON(path, body = {}) {
  const res = await fetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw await responseError(res)
  return res.json()
}

// `pastWeeks` asks for that many finished shops alongside the planning window.
export function fetchSchedule(pastWeeks = 0) {
  return getJSON(`/api/schedule?past_weeks=${pastWeeks}`)
}

// A partial update: only the fields passed are changed.
export function updateScheduleSettings(settings) {
  return putJSON('/api/schedule/settings', settings)
}

export function setWeekSkipped({ weekStart, skipped }) {
  return putJSON(`/api/schedule/weeks/${weekStart}`, { skipped })
}
