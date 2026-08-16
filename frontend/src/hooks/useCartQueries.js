import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  fetchCartBasket,
  fetchCartStatus,
  logoutCart,
  planCartBasket,
  pushCartBasket,
  refreshCartSession,
  startCartLogin,
  submitCartOtp,
} from '../api/cartClient.js'

// The retailer leads every query key. Two shops have their own carts, ledgers
// and logins, and a key that left it out would serve Ocado's cached connection
// status under a Sainsbury's chip the moment you switched shops.
//
// The account does not appear in any key, because the server no longer takes
// one: every response is about the caller's own account at that shop.
const key = (name, retailer, ...rest) => ['cart', retailer, name, ...rest]

// ``active`` polls fast while a login is in flight. That request can block for
// minutes — Ocado launches a browser and then waits for an emailed code — and
// the stage it reports only moves in the meantime, so a 30s poll would miss
// most of it.
export function useCartStatus(retailer, { enabled = true, active = false } = {}) {
  return useQuery({
    queryKey: key('status', retailer),
    queryFn: () => fetchCartStatus(retailer),
    enabled: enabled && Boolean(retailer),
    refetchInterval: active ? 2_000 : 30_000,
  })
}

function useStatusWriteback(retailer) {
  const qc = useQueryClient()
  return (data) => qc.setQueryData(key('status', retailer), data)
}

export function useCartLogin(retailer) {
  const onDone = useStatusWriteback(retailer)
  return useMutation({
    mutationFn: ({ email, password }) => startCartLogin({ retailer, email, password }),
    onSuccess: onDone,
  })
}

// Reconnects without any user input where it can. Only useCartLogin receives
// credentials and may therefore escalate to a full login and emailed code.
export function useCartSessionRefresh(retailer) {
  const onDone = useStatusWriteback(retailer)
  return useMutation({
    mutationFn: () => refreshCartSession(retailer),
    onSuccess: onDone,
  })
}

export function useCartOtp(retailer) {
  const onDone = useStatusWriteback(retailer)
  return useMutation({
    mutationFn: (code) => submitCartOtp({ retailer, code }),
    onSuccess: onDone,
  })
}

export function useCartLogout(retailer) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => logoutCart(retailer),
    onSuccess: (data) => {
      qc.setQueryData(key('status', retailer), data)
      qc.invalidateQueries({ queryKey: ['cart', retailer] })
    },
  })
}

export function useCartPush(retailer) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (vars) => pushCartBasket({ retailer, ...vars }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key('basket', retailer) })
      // A push re-checks stock and may swap packs, so the priced basket the
      // page is showing is out of date the moment it returns.
      qc.invalidateQueries({ queryKey: ['planner-basket'] })
      // The cart and the ledger both moved, so the preview is answering an
      // old question.
      qc.invalidateQueries({ queryKey: key('plan', retailer) })
    },
  })
}

// The preview of what a push would do. A query rather than a mutation: it
// changes nothing, and it should follow the week around as you edit it. Costs
// one cart read, so it waits for a connection rather than failing without one.
export function useCartPushPlan(
  { retailer, selections, ownedItemKeys, packOverrides, snapOverrides },
  { enabled = true } = {},
) {
  return useQuery({
    queryKey: key('plan', retailer, selections, ownedItemKeys, packOverrides, snapOverrides),
    queryFn: () =>
      planCartBasket({
        retailer,
        selections,
        ownedItemKeys,
        packOverrides,
        snapOverrides,
      }),
    enabled: enabled && Boolean(retailer),
  })
}

export function useCartBasket(retailer, { enabled = true } = {}) {
  return useQuery({
    queryKey: key('basket', retailer),
    queryFn: () => fetchCartBasket(retailer),
    enabled: enabled && Boolean(retailer),
  })
}
