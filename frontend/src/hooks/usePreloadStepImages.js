import { useEffect } from 'react'

// Retain the preload links for this browser session. The cooking screen only
// displays one image at a time, so fetching the small set of step photos as
// soon as a recipe opens avoids a visible image swap on every next-step tap.
const preloadedUrls = new Set()

export function usePreloadStepImages(steps, { enabled = true } = {}) {
  useEffect(() => {
    if (!enabled) return undefined
    let cancelled = false
    const preloadSteps = () => {
      if (cancelled) return
      for (const step of steps ?? []) {
        const href = step.image_url
        if (!href || preloadedUrls.has(href)) continue

        const preload = document.createElement('link')
        preload.rel = 'preload'
        preload.as = 'image'
        preload.href = href
        preload.fetchPriority = 'low'
        document.head.append(preload)
        preloadedUrls.add(href)
      }
    }

    const idleId = window.requestIdleCallback
      ? window.requestIdleCallback(preloadSteps)
      : window.setTimeout(preloadSteps, 1)
    return () => {
      cancelled = true
      if (window.cancelIdleCallback) window.cancelIdleCallback(idleId)
      else window.clearTimeout(idleId)
    }
  }, [enabled, steps])
}
