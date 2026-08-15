export async function fetchAccount() {
  const response = await fetch('/api/account')
  const contentType = response.headers.get('content-type') ?? ''
  const isJSON = contentType.includes('application/json')

  if (response.ok && isJSON) return response.json()

  // An older backend does not know this route and lets the SPA fallback answer
  // it with index.html and a misleading 200. Name that case instead of leaking
  // a JSON parser error about the first "<" in <!doctype html>.
  if (response.ok) {
    throw new Error('The account API is unavailable. Restart the backend and try again.')
  }

  let detail = null
  if (isJSON) {
    try {
      detail = (await response.json())?.detail
    } catch {
      // Keep the status-only fallback for malformed JSON responses.
    }
  }
  throw new Error(detail ? `HTTP ${response.status}: ${detail}` : `HTTP ${response.status}`)
}
