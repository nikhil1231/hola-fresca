import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  fetchOcadoAccounts,
  fetchOcadoBasket,
  fetchOcadoSlots,
  fetchOcadoStatus,
  planOcadoBasket,
  pushOcadoBasket,
  refreshOcadoSession,
  refreshOcadoStock,
  reserveOcadoSlot,
  startOcadoLogin,
  submitOcadoOtp,
} from '../api/ocadoClient.js'

export function useOcadoAccounts() {
  return useQuery({
    queryKey: ['ocado-accounts'],
    queryFn: fetchOcadoAccounts,
  })
}

// ``active`` polls fast while a login is in flight. That request blocks for
// minutes - a browser launch, then a wait for the emailed code - and the stage
// it reports only moves in the meantime, so a 30s poll would miss most of it.
export function useOcadoStatus(accountId, { enabled = true, active = false } = {}) {
  return useQuery({
    queryKey: ['ocado-status', accountId],
    queryFn: () => fetchOcadoStatus(accountId),
    enabled: enabled && Boolean(accountId),
    refetchInterval: active ? 2_000 : 30_000,
  })
}

export function useOcadoLogin(accountId) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => startOcadoLogin(accountId),
    onSuccess: (data) => {
      qc.setQueryData(['ocado-status', data.account_id], data)
      qc.invalidateQueries({ queryKey: ['ocado-accounts'] })
    },
  })
}

// Reconnects without any user input where it can. Distinct from useOcadoLogin,
// which may escalate to a password login and email an OTP.
export function useOcadoSessionRefresh(accountId) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => refreshOcadoSession(accountId),
    onSuccess: (data) => {
      qc.setQueryData(['ocado-status', data.account_id], data)
      qc.invalidateQueries({ queryKey: ['ocado-accounts'] })
    },
  })
}

export function useOcadoOtp(accountId) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (code) => submitOcadoOtp({ accountId, code }),
    onSuccess: (data) => {
      qc.setQueryData(['ocado-status', data.account_id], data)
      qc.invalidateQueries({ queryKey: ['ocado-accounts'] })
    },
  })
}

export function useOcadoPush(accountId) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: pushOcadoBasket,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ocado-basket', accountId] })
      // A push re-checks stock and may swap packs, so the priced basket the
      // page is showing is out of date the moment it returns.
      qc.invalidateQueries({ queryKey: ['planner-basket'] })
      // The cart and the ledger both moved, so the preview is answering an
      // old question.
      qc.invalidateQueries({ queryKey: ['ocado-plan', accountId] })
    },
  })
}

// The preview of what a push would do. A query rather than a mutation: it
// changes nothing, and it should follow the week around as you edit it. Costs
// one cart read, so it waits for a connection rather than failing without one.
export function useOcadoPushPlan(
  { accountId, selections, ownedItemKeys, packOverrides },
  { enabled = true } = {},
) {
  return useQuery({
    queryKey: ['ocado-plan', accountId, selections, ownedItemKeys, packOverrides],
    queryFn: () => planOcadoBasket({ accountId, selections, ownedItemKeys, packOverrides }),
    enabled: enabled && Boolean(accountId) && selections.length > 0,
  })
}

// Refreshing stock rewrites prices and availability in the catalogue, which
// re-covers every affected ingredient - so the basket has to be re-fetched, not
// just re-rendered.
export function useOcadoStockRefresh() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: refreshOcadoStock,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['planner-basket'] }),
  })
}

export function useOcadoBasket(accountId, { enabled = true } = {}) {
  return useQuery({
    queryKey: ['ocado-basket', accountId],
    queryFn: () => fetchOcadoBasket(accountId),
    enabled: enabled && Boolean(accountId),
  })
}

export function useOcadoSlots(params, { enabled = true } = {}) {
  return useQuery({
    queryKey: ['ocado-slots', params],
    queryFn: () => fetchOcadoSlots(params),
    enabled: enabled && Boolean(params?.accountId),
  })
}

export function useOcadoReserve() {
  return useMutation({
    mutationFn: reserveOcadoSlot,
  })
}
