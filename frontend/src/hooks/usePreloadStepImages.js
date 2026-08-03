import { useEffect } from 'react'

// Retain the preload links for this browser session. The cooking screen only
// displays one image at a time, so fetching the small set of step photos as
// soon as a recipe opens avoids a visible image swap on every next-step tap.
const preloadedUrls = new Set()

export function usePreloadStepImages(steps) {
  useEffect(() => {
    for (const step of steps ?? []) {
      const href = step.image_url
      if (!href || preloadedUrls.has(href)) continue

      const preload = document.createElement('link')
      preload.rel = 'preload'
      preload.as = 'image'
      preload.href = href
      preload.fetchPriority = 'high'
      document.head.append(preload)
      preloadedUrls.add(href)
    }
  }, [steps])
}
