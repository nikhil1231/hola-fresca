import { useCallback, useEffect, useRef, useState } from 'react'
import { request } from './api.js'

export const isReady = (auth) => auth?.status === 'ready'
const queueKey = (queue) => JSON.stringify(queue)
const scope = (auth) => ({ account_id: auth?.account_id ?? null, account_version: auth?.account_version ?? 0 })
const sameAccount = (left, right) => left?.account_id === right?.account_id && left?.account_version === right?.account_version

export function useSharedShop() {
  const [auth, setAuth] = useState(null)
  const [state, setState] = useState(null)
  const [queue, setQueue] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [stale, setStale] = useState(false)
  const [accessExpired, setAccessExpired] = useState(false)
  const refs = useRef({ epoch: 0, mounted: false, writing: false, refreshing: false, refreshAgain: false, auth: null, queue: null, state: null })

  const reportError = useCallback((failure) => {
    if (!refs.current.mounted || failure.name === 'AbortError') return
    setError(failure.message)
    if (failure.status === 401 || failure.status === 403) setAccessExpired(true)
  }, [])

  // State and queue reads can finish in either order. Never replace a newer
  // server queue with the older queue embedded in a slow basket response.
  const acceptQueue = useCallback((next) => {
    if (!next) return false
    const current = refs.current.queue
    if (current?.target_id === next.target_id && current?.updated_at && next.updated_at && Date.parse(next.updated_at) < Date.parse(current.updated_at)) return false
    if (queueKey(current) === queueKey(next)) return false
    refs.current.queue = next
    setQueue(next)
    return true
  }, [])

  const refresh = useCallback(async () => {
    const ref = refs.current
    if (!isReady(ref.auth) || ref.writing) return
    if (ref.refreshing) {
      ref.refreshAgain = true
      return
    }
    ref.refreshing = true
    setSyncing(true)
    const epoch = ref.epoch
    try {
      const next = await request('/state')
      if (!ref.mounted || ref.epoch !== epoch) return
      if (!sameAccount(next, ref.auth)) { setStale(true); return }
      // A queue poll may have observed edits made during this state read. Read
      // again before allowing quantity edits based on an obsolete projection.
      const current = ref.queue
      if (current?.target_id === next.queue?.target_id && current?.updated_at && next.queue?.updated_at && Date.parse(next.queue.updated_at) < Date.parse(current.updated_at)) {
        ref.refreshAgain = true
        return
      }
      ref.state = next
      setState(next)
      acceptQueue(next.queue)
      setStale(false)
    } catch (failure) {
      if (ref.epoch === epoch) {
        setStale(true)
        reportError(failure)
      }
    } finally {
      ref.refreshing = false
      if (ref.mounted) {
        setSyncing(false)
        setLoading(false)
        if (ref.refreshAgain) {
          ref.refreshAgain = false
          void refresh()
        }
      }
    }
  }, [acceptQueue, reportError])

  const acceptAuth = useCallback((next) => {
    const ref = refs.current
    const wasReady = isReady(ref.auth)
    const changedAccount = !sameAccount(ref.auth, next)
    ref.auth = next
    setAuth(next)
    setAccessExpired(false)
    if (changedAccount || wasReady !== isReady(next)) ref.epoch += 1
    if (changedAccount) {
      ref.queue = null
      ref.state = null
      setState(null)
      setQueue(null)
      setError(null)
      setStale(false)
      setLoading(isReady(next))
    }
    if (!isReady(next)) {
      ref.queue = null
      ref.state = null
      setState(null)
      setQueue(null)
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const ref = refs.current
    ref.mounted = true
    let polling = false
    let disposed = false
    let refreshedSession = null
    async function poll() {
      if (polling || ref.writing || disposed) return
      polling = true
      let epoch = ref.epoch
      try {
        let nextAuth = await request('/status')
        if (disposed || ref.epoch !== epoch) return
        const accountKey = JSON.stringify(scope(nextAuth))
        if (refreshedSession !== accountKey && nextAuth.email && !isReady(nextAuth) && !['awaiting_otp', 'otp_required'].includes(nextAuth.status)) {
          refreshedSession = accountKey
          nextAuth = await request('/session/refresh', { body: scope(nextAuth) })
        }
        if (disposed || ref.epoch !== epoch) return
        const wasReady = isReady(ref.auth)
        acceptAuth(nextAuth)
        epoch = ref.epoch
        if (isReady(nextAuth)) {
          const readEpoch = ref.epoch
          const nextQueue = await request('/queue')
          if (disposed || ref.epoch !== readEpoch || !sameAccount(nextQueue, ref.auth)) return
          const changed = acceptQueue(nextQueue)
          if (changed || !wasReady || !ref.state) void refresh()
        }
      } catch (failure) {
        if (!disposed && ref.epoch === epoch) {
          reportError(failure)
          setLoading(false)
        }
      } finally {
        polling = false
      }
    }
    void poll()
    const interval = window.setInterval(poll, 3000)
    window.addEventListener('online', poll)
    window.addEventListener('focus', poll)
    return () => {
      disposed = true
      ref.mounted = false
      ref.epoch += 1
      window.clearInterval(interval)
      window.removeEventListener('online', poll)
      window.removeEventListener('focus', poll)
    }
  }, [acceptAuth, acceptQueue, refresh, reportError])

  const mutate = useCallback(async (path, body, authMutation = false) => {
    const ref = refs.current
    if (ref.writing) return false
    ref.writing = true
    ref.epoch += 1
    setBusy(true)
    setError(null)
    let accepted = false
    try {
      const next = await request(path, { body: { ...scope(ref.auth), ...body } })
      if (!ref.mounted) return false
      if (authMutation) acceptAuth(next)
      else acceptQueue(next)
      accepted = true
    } catch (failure) {
      reportError(failure)
      // Never retry a write: a dropped response may already have queued it.
      setStale(true)
    } finally {
      ref.writing = false
      if (ref.mounted) {
        setBusy(false)
        if (isReady(ref.auth)) void refresh()
      }
    }
    return accepted
  }, [acceptAuth, acceptQueue, refresh, reportError])

  const refreshShop = useCallback(async () => {
    const ref = refs.current
    if (ref.writing) return
    if (!isReady(ref.auth)) {
      const epoch = ref.epoch
      try {
        const next = await request('/status')
        if (!ref.mounted || ref.epoch !== epoch) return
        acceptAuth(next)
      } catch (failure) {
        reportError(failure)
        return
      }
    }
    await refresh()
  }, [acceptAuth, refresh, reportError])

  const action = (body) => {
    const ref = refs.current
    if (ref.writing || ref.refreshing || !isReady(ref.auth)) return Promise.resolve(false)
    return mutate('/actions', { ...scope(auth), ...body, target_id: body.target_id ?? ref.state?.queue?.target_id })
  }
  const cancel = (selection = {}) => mutate('/queue/cancel', { ...scope(auth), ...selection, target_id: selection.target_id ?? refs.current.queue?.target_id })
  const authenticate = (path, body = {}) => mutate(path, { ...scope(auth), ...body }, true)
  const locked = busy || syncing || stale || !state || !queue?.target_id || queue.status === 'running' || queue.status === 'failed' || accessExpired
  const selectAccount = (id) => mutate('/account', { ...scope(auth), selected_account_id: Number(id) }, true)
  return { selection: scope(auth), selectAccount, auth, state, queue, error, busy, loading, syncing, stale, accessExpired, locked, refresh: refreshShop, action, cancel, authenticate, reportError, clearError: () => setError(null) }
}
