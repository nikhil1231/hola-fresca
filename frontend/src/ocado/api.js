const prefix = '/api/ocado-shared'

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export async function request(path, { body, signal } = {}) {
  let response
  try {
    response = await fetch(`${prefix}${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal,
    })
  } catch (error) {
    if (error.name === 'AbortError') throw error
    throw new ApiError('Cannot reach the shared shop. Check your connection, then refresh. Your changes have not been retried.')
  }
  if (response.status === 401 || response.status === 403) {
    throw new ApiError('Your access session has expired or this account is not authorised. Sign in again to continue.', response.status)
  }
  if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) {
    throw new ApiError('The server did not return shop data. Your access session may have expired. Sign in again, or try refreshing shortly.', 401)
  }
  const data = await response.json()
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : data.detail?.message
    throw new ApiError(detail || `The request could not be completed (HTTP ${response.status}).`, response.status)
  }
  return data
}
