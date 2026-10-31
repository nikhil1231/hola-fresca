import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { IconCheck, IconInfoCircle, IconX } from '@tabler/icons-react'

import { ToastContext, useToasts } from './toastContext.js'

function Toast({ item, dismiss }) {
  useEffect(() => {
    if (item.duration === 0) return
    const timer = window.setTimeout(() => dismiss(item.key), item.duration ?? (item.type === 'error' ? 9000 : 4500))
    return () => window.clearTimeout(timer)
  }, [item, dismiss])
  const Icon = item.type === 'success' ? IconCheck : IconInfoCircle
  return <div className={`shop-toast toast-${item.type || 'info'}`} role={item.type === 'error' ? 'alert' : 'status'}>
    <Icon size={20} aria-hidden="true" /><span>{item.message}</span>
    {item.action && <button type="button" className="toast-action" onClick={() => { item.action.onClick(); dismiss(item.key) }}>{item.action.label}</button>}
    <button type="button" className="icon-button" aria-label="Dismiss notification" onClick={() => dismiss(item.key)}><IconX size={18} /></button>
  </div>
}

export function ToastProvider({ children }) {
  const [items, setItems] = useState([])
  const [portalHost] = useState(() => document.createElement('div'))
  const popover = useRef(null)
  const visible = useRef(false)
  const nextId = useRef(0)
  const dismiss = useCallback((key) => setItems((current) => current.filter((item) => item.key !== key)), [])
  const notify = useCallback((message, options = {}) => {
    const item = { ...options, message, id: ++nextId.current, key: options.key || `toast-${nextId.current}` }
    setItems((current) => [...current.filter((old) => old.key !== item.key), item].slice(-3))
  }, [])
  useEffect(() => {
    const position = () => {
      // A modal makes elements outside it inert. Keep the toast inside the
      // active dialog so its dismissal and recovery buttons remain usable.
      const target = [...document.querySelectorAll('dialog[open]')].at(-1) || document.body
      if (portalHost.parentNode === target) return
      if (popover.current?.matches(':popover-open')) popover.current.hidePopover()
      target.append(portalHost)
      if (visible.current) popover.current?.showPopover?.()
    }
    const observer = new MutationObserver(position)
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['open'] })
    position()
    return () => { observer.disconnect(); portalHost.remove() }
  }, [portalHost])
  useEffect(() => {
    // Native popovers keep feedback visible above an open details dialog too.
    visible.current = items.length > 0
    if (!popover.current?.isConnected) return
    popover.current?.hidePopover?.()
    if (items.length) popover.current?.showPopover?.()
  }, [items])
  return <ToastContext.Provider value={{ notify, dismiss }}>{children}{createPortal(
    <div ref={popover} className="toast-region" popover="manual" aria-label="Notifications">{items.map((item) => <Toast key={item.id} item={item} dismiss={dismiss} />)}</div>, portalHost,
  )}</ToastContext.Provider>
}

// Feedback has no footprint in the page layout and is announced once.
export function ToastMessage({ message, toastKey, type = 'info', duration }) {
  const { notify, dismiss } = useToasts()
  useEffect(() => {
    const key = toastKey || message
    notify(message, { key, type, duration })
    return () => dismiss(key)
  }, [message, toastKey, type, duration, notify, dismiss])
  return null
}
