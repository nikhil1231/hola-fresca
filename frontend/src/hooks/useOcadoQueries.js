import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  fetchOcadoBasket,
  fetchOcadoSlots,
  fetchOcadoStatus,
  pushOcadoBasket,
  reserveOcadoSlot,
  startOcadoLogin,
  submitOcadoOtp,
} from '../api/ocadoClient.js'

export function useOcadoStatus() {
  return useQuery({
    queryKey: ['ocado-status'],
    queryFn: fetchOcadoStatus,
    refetchInterval: 30_000,
  })
}

export function useOcadoLogin() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: startOcadoLogin,
    onSuccess: (data) => qc.setQueryData(['ocado-status'], data),
  })
}

export function useOcadoOtp() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: submitOcadoOtp,
    onSuccess: (data) => qc.setQueryData(['ocado-status'], data),
  })
}

export function useOcadoPush() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: pushOcadoBasket,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ocado-basket'] }),
  })
}

export function useOcadoBasket({ enabled = true } = {}) {
  return useQuery({
    queryKey: ['ocado-basket'],
    queryFn: fetchOcadoBasket,
    enabled,
  })
}

export function useOcadoSlots(params, { enabled = true } = {}) {
  return useQuery({
    queryKey: ['ocado-slots', params],
    queryFn: () => fetchOcadoSlots(params),
    enabled,
  })
}

export function useOcadoReserve() {
  return useMutation({
    mutationFn: reserveOcadoSlot,
  })
}

