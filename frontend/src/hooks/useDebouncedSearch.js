import { useEffect, useRef, useState } from 'react'

// Keep the live input independent from the URL/query lifecycle. In particular,
// an older value reaching the URL after a newer character has been typed must
// not be copied back over the newer local value.
export function useDebouncedSearch(externalValue, onCommit, delay = 500) {
  const [value, setValue] = useState(externalValue)
  const latestExternalValue = useRef(externalValue)
  const pendingCommit = useRef(null)
  const commit = useRef(onCommit)

  latestExternalValue.current = externalValue
  commit.current = onCommit

  useEffect(() => {
    if (pendingCommit.current === externalValue) {
      pendingCommit.current = null
      return
    }

    // A different value came from browser navigation, a link, or another
    // control, rather than from this input's own last commit.
    pendingCommit.current = null
    setValue(externalValue)
  }, [externalValue])

  useEffect(() => {
    const handle = setTimeout(() => {
      if (value === latestExternalValue.current) return
      pendingCommit.current = value
      commit.current(value)
    }, delay)

    return () => clearTimeout(handle)
  }, [delay, value])

  return [value, setValue]
}
