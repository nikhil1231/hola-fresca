import { useEffect, useMemo, useRef, useState } from 'react'

import {
  useCartAccounts,
  useCartLogin,
  useCartLogout,
  useCartOtp,
  useCartSessionRefresh,
  useCartStatus,
} from './useCartQueries.js'

// Remembered per shop. One key for both would offer Sainsbury's an Ocado
// account id on the first render after a switch.
const accountStorageKey = (retailer) => `holafresca:cart-account-id:${retailer}`
const manualLogoutStorageKey = (retailer, accountId) =>
  `holafresca:cart-manual-logout:${retailer}:${accountId}`

/** The complete retailer sign-in flow shared by Settings and Checkout. */
export function useCartConnection(retailer) {
  const accounts = useCartAccounts(retailer)
  const [accountId, setAccountId] = useState(null)
  const selectedAccount = useMemo(
    () => (accounts.data?.items ?? []).find((account) => account.id === accountId) ?? null,
    [accounts.data?.items, accountId],
  )
  const login = useCartLogin(retailer, accountId)
  const logout = useCartLogout(retailer)
  const sessionRefresh = useCartSessionRefresh(retailer, accountId)
  const status = useCartStatus(retailer, accountId, {
    enabled: Boolean(selectedAccount),
    active: login.isPending || sessionRefresh.isPending,
  })
  const otp = useCartOtp(retailer, accountId)
  const reconnectAttempted = useRef(new Set())
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

  useEffect(() => {
    const items = accounts.data?.items ?? []
    if (!items.length) return
    if (accountId && items.some((account) => account.id === accountId)) return
    const remembered = window.localStorage.getItem(accountStorageKey(retailer))
    const next = items.some((account) => account.id === remembered)
      ? remembered
      : accounts.data?.default_account_id ?? items[0].id
    setAccountId(next)
    window.localStorage.setItem(accountStorageKey(retailer), next)
  }, [accountId, accounts.data, retailer])

  useEffect(() => {
    setLoginEmail(selectedAccount?.email ?? '')
    setLoginPassword('')
  }, [retailer, accountId, selectedAccount?.email])

  useEffect(() => {
    if (!accountId || !retailer) return
    window.localStorage.setItem(accountStorageKey(retailer), accountId)
    setOtpCode('')
    actions.current.resetLogin()
    actions.current.resetLogout()
    actions.current.resetOtp()
  }, [accountId, retailer])

  useEffect(() => {
    if (!accountId || !retailer || status.data?.status !== 'logged_out') return
    const key = `${retailer}:${accountId}`
    if (window.localStorage.getItem(manualLogoutStorageKey(retailer, accountId)) === '1') return
    if (reconnectAttempted.current.has(key)) return
    reconnectAttempted.current.add(key)
    actions.current.refreshSession()
  }, [accountId, retailer, status.data?.status])

  const submitLogin = () => {
    window.localStorage.removeItem(manualLogoutStorageKey(retailer, accountId))
    login.mutate(
      { email: loginEmail.trim(), password: loginPassword },
      { onSettled: () => setLoginPassword('') },
    )
  }

  const disconnect = () => {
    if (!retailer || !accountId) return
    const storageKey = manualLogoutStorageKey(retailer, accountId)
    window.localStorage.setItem(storageKey, '1')
    logout.mutate(undefined, {
      onSuccess: (data) => {
        window.localStorage.setItem(manualLogoutStorageKey(retailer, data.account_id), '1')
      },
      onError: () => window.localStorage.removeItem(storageKey),
    })
  }

  const stage = status.data?.stage ?? 'idle'
  return {
    accountId,
    accounts,
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
    selectedAccount,
    setAccountId,
    setLoginEmail,
    setLoginPassword,
    setOtpCode,
    stage,
    status,
    submitLogin,
  }
}
