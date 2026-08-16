import { useEffect, useRef, useState } from 'react'

import {
  useCartLogin,
  useCartLogout,
  useCartOtp,
  useCartSessionRefresh,
  useCartStatus,
} from './useCartQueries.js'

// Remembered per shop, and only to stop the automatic reconnect below from
// undoing a sign-out the moment the page re-renders. No account id is stored:
// there is one connection per person per shop, and the server knows which.
const manualLogoutStorageKey = (retailer) => `holafresca:cart-manual-logout:${retailer}`

// Which shops have had their one automatic reconnect this page load.
//
// Module scope rather than a ref, and that is the whole point: a ref is reborn
// with the component, so anything that remounts the panel — switching tabs,
// switching shops and back, a parent re-keying — bought another "reconnect
// once". Each of those attempts leaves the mutation pending for its duration,
// so a component that remounts often enough never leaves the pending state and
// the panel sits on "Reusing your saved session…" while the status poll runs at
// its in-flight rate forever. The reconnect is a fact about the page, not about
// a component instance, so it is stored where the page can see it.
const reconnected = new Set()

/** The complete retailer sign-in flow shared by Settings and Checkout. */
export function useCartConnection(retailer) {
  const login = useCartLogin(retailer)
  const logout = useCartLogout(retailer)
  const sessionRefresh = useCartSessionRefresh(retailer)
  // Only a *login* earns the fast poll. It blocks for minutes behind a browser
  // launch and an emailed code, and the stage it reports moves in the meantime,
  // so polling is the only way to show progress. A session refresh is a second
  // at most and writes the status back itself when it lands, so polling it
  // added nothing but a request every two seconds for as long as it ran.
  const status = useCartStatus(retailer, { active: login.isPending })
  const otp = useCartOtp(retailer)
  const actions = useRef(null)
  actions.current = {
    resetLogin: login.reset,
    resetLogout: logout.reset,
    resetOtp: otp.reset,
    refreshSession: sessionRefresh.mutate,
  }
  const [otpCode, setOtpCode] = useState('')
  const [loginEmail, setLoginEmail] = useState('')
  const [loginPassword, setLoginPassword] = useState('')

  // The address comes back from the server, so a returning user finds the form
  // already filled in with the account they connected. The password never does.
  const knownEmail = status.data?.email ?? ''
  useEffect(() => {
    setLoginEmail(knownEmail)
    setLoginPassword('')
  }, [retailer, knownEmail])

  useEffect(() => {
    if (!retailer) return
    setOtpCode('')
    actions.current.resetLogin()
    actions.current.resetLogout()
    actions.current.resetOtp()
  }, [retailer])

  useEffect(() => {
    if (!retailer || status.data?.status !== 'logged_out') return
    if (window.localStorage.getItem(manualLogoutStorageKey(retailer)) === '1') return
    if (reconnected.has(retailer)) return
    reconnected.add(retailer)
    actions.current.refreshSession()
  }, [retailer, status.data?.status])

  const submitLogin = () => {
    window.localStorage.removeItem(manualLogoutStorageKey(retailer))
    // Signing in by hand is an explicit second chance: let the automatic
    // reconnect run again if this login leaves the session logged out.
    reconnected.delete(retailer)
    login.mutate(
      { email: loginEmail.trim(), password: loginPassword },
      { onSettled: () => setLoginPassword('') },
    )
  }

  const disconnect = () => {
    if (!retailer) return
    const storageKey = manualLogoutStorageKey(retailer)
    window.localStorage.setItem(storageKey, '1')
    logout.mutate(undefined, {
      onError: () => window.localStorage.removeItem(storageKey),
    })
  }

  const stage = status.data?.stage ?? 'idle'
  return {
    awaitingOtp: status.data?.status === 'awaiting_otp',
    connected: status.data?.status === 'ready',
    disconnect,
    handlingCode: stage === 'waiting_for_code' || stage === 'entering_code',
    login,
    loginEmail,
    loginPassword,
    logout,
    otp,
    otpCode,
    reconnecting: sessionRefresh.isPending,
    setLoginEmail,
    setLoginPassword,
    setOtpCode,
    stage,
    status,
    submitLogin,
  }
}
