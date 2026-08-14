// Keeping the app usable when the Cloudflare Access session lapses.
//
// Access sessions expire on a fixed clock, and when one does the edge stops
// answering this app's API calls. Left alone that shows up as every panel on the
// page failing at once, with no hint that signing in again is the fix — the tab
// looks broken rather than logged out.
//
// The shape of the fix is forced by how the edge answers. By default an expired
// request is met with a 302 to the login page on cloudflareaccess.com: a
// cross-origin redirect that fetch dutifully follows and then cannot read, so it
// surfaces as a bare TypeError indistinguishable from the laptop being offline.
// Sending `X-Requested-With: XMLHttpRequest` changes that answer to a plain 401
// on our own origin, which is unambiguous — the HolaFresca API never returns 401
// itself, so nothing else can produce one.
//
// Recovering has to be a *top-level* navigation, because only a document load
// can follow the redirect chain out to Google and back; an XHR cannot. Hence
// reloading the page rather than anything subtler. The reload is a no-op for the
// LAN and dev, where nothing ever answers 401.

// How recently a reload must have happened for another one to be suppressed. It
// exists to stop a loop: if a 401 somehow survives re-authentication, the page
// should settle showing errors rather than reload forever.
const RELOAD_DEBOUNCE_MS = 15_000
const RELOAD_MARKER = 'holafresca:access-reload-at'

// sessionStorage throws rather than degrading in a few real situations (Safari
// private browsing, storage disabled). None of them should take the app down.
function readMarker() {
  try {
    return Number(window.sessionStorage.getItem(RELOAD_MARKER)) || 0
  } catch {
    return 0
  }
}

function writeMarker(value) {
  try {
    if (value == null) window.sessionStorage.removeItem(RELOAD_MARKER)
    else window.sessionStorage.setItem(RELOAD_MARKER, String(value))
  } catch {
    // Without the marker the debounce is gone but the reload still works, which
    // is the right way round to fail.
  }
}

// Which requests this applies to: our own API, not third-party URLs that happen
// to pass through fetch. Accepts the string/URL/Request shapes fetch does.
function isApiRequest(input) {
  const raw =
    typeof input === 'string' ? input : input instanceof URL ? input.href : input?.url
  if (typeof raw !== 'string') return false
  try {
    const url = new URL(raw, window.location.origin)
    return url.origin === window.location.origin && url.pathname.startsWith('/api/')
  } catch {
    return false
  }
}

function reauthenticate() {
  const now = Date.now()
  if (now - readMarker() < RELOAD_DEBOUNCE_MS) return
  writeMarker(now)
  window.location.reload()
}

/**
 * Wrap window.fetch so an expired Access session sends the tab back through
 * sign-in instead of silently failing.
 *
 * Deliberately global rather than a wrapper the API clients call. There are six
 * client modules and no single choke point, and a cross-cutting concern that
 * each of them has to remember to opt into is one that a seventh will miss.
 */
export function installAccessSessionHandling() {
  const nativeFetch = window.fetch.bind(window)

  window.fetch = async (input, init) => {
    if (!isApiRequest(input)) return nativeFetch(input, init)

    // Seed from the Request's own headers when one was passed, so wrapping does
    // not drop headers the caller set.
    const headers = new Headers(
      init?.headers ?? (input instanceof Request ? input.headers : undefined),
    )
    headers.set('X-Requested-With', 'XMLHttpRequest')

    const response = await nativeFetch(input, { ...init, headers })

    if (response.status === 401) {
      reauthenticate()
    } else if (response.ok && readMarker()) {
      // Signed back in. Clear the marker so the next expiry is acted on at once
      // rather than being swallowed by a debounce left over from this one.
      writeMarker(null)
    }

    return response
  }
}
