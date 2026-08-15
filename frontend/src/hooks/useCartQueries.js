import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  fetchCartAccounts,
  fetchCartBasket,
  fetchCartStatus,
  planCartBasket,
  pushCartBasket,
  refreshCartSession,
  startCartLogin,
  submitCartOtp,
} from '../api/cartClient.js'

// The retailer leads every query key. Two shops have their own carts, ledgers
// and logins, and a key that left it out would serve Ocado's cached connection
// status under a Sainsbury's chip the moment you switched shops.
const key = (name, retailer, ...rest) => ['cart', retailer, name, ...rest]

export function useCartAccounts(retailer) {
  return useQuery({
    queryKey: key('accounts', retailer),
    queryFn: () => fetchCartAccounts(retailer),
    enabled: Boolean(retailer),
  })
}

// ``active`` polls fast while a login is in flight. That request can block for
// minutes — Ocado launches a browser and then waits for an emailed code — and
// the stage it reports only moves in the meantime, so a 30s poll would miss
// most of it.
export function useCartStatus(retailer, accountId, { enabled = true, active = false } = {}) {
  return useQuery({
    queryKey: key('status', retailer, accountId),
    queryFn: () => fetchCartStatus(retailer, accountId),
    enabled: enabled && Boolean(retailer) && Boolean(accountId),
    refetchInterval: active ? 2_000 : 30_000,
  })
}

function useStatusWriteback(retailer) {
  const qc = useQueryClient()
  return (data) => {
    qc.setQueryData(key('status', retailer, data.account_id), data)
    qc.invalidateQueries({ queryKey: key('accounts', retailer) })
  }
}

export function useCartLogin(retailer, accountId) {
  const onDone = useStatusWriteback(retailer)
  return useMutation({
    mutationFn: () => startCartLogin(retailer, accountId),
    onSuccess: onDone,
  })
}

// Reconnects without any user input where it can. Distinct from useCartLogin,
// which may escalate to a password login and email a one-time code.
export function useCartSessionRefresh(retailer, accountId) {
  const onDone = useStatusWriteback(retailer)
  return useMutation({
    mutationFn: () => refreshCartSession(retailer, accountId),
    onSuccess: onDone,
  })
}

export function useCartOtp(retailer, accountId) {
  const onDone = useStatusWriteback(retailer)
  return useMutation({
    mutationFn: (code) => submitCartOtp({ retailer, accountId, code }),
    onSuccess: onDone,
  })
}

export function useCartPush(retailer, accountId) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (vars) => pushCartBasket({ retailer, ...vars }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key('basket', retailer, accountId) })
      // A push re-checks stock and may swap packs, so the priced basket the
      // page is showing is out of date the moment it returns.
      qc.invalidateQueries({ queryKey: ['planner-basket'] })
      // The cart and the ledger both moved, so the preview is answering an
      // old question.
      qc.invalidateQueries({ queryKey: key('plan', retailer, accountId) })
    },
  })
}

// The preview of what a push would do. A query rather than a mutation: it
// changes nothing, and it should follow the week around as you edit it. Costs
// one cart read, so it waits for a connection rather than failing without one.
export function useCartPushPlan(
  { retailer, accountId, selections, ownedItemKeys, packOverrides, snapOverrides },
  { enabled = true } = {},
) {
  return useQuery({
    queryKey: key(
      'plan',
      retailer,
      accountId,
      selections,
      ownedItemKeys,
      packOverrides,
      snapOverrides,
    ),
    queryFn: () =>
      planCartBasket({
        retailer,
        accountId,
        selections,
        ownedItemKeys,
        packOverrides,
        snapOverrides,
      }),
    enabled: enabled && Boolean(retailer) && Boolean(accountId),
  })
}

export function useCartBasket(retailer, accountId, { enabled = true } = {}) {
  return useQuery({
    queryKey: key('basket', retailer, accountId),
    queryFn: () => fetchCartBasket(retailer, accountId),
    enabled: enabled && Boolean(retailer) && Boolean(accountId),
  })
}
