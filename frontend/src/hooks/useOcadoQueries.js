import { useMutation, useQuery } from '@tanstack/react-query'

import { fetchOcadoSlots, reserveOcadoSlot } from '../api/ocadoClient.js'

// Delivery slots, which only Ocado has. The session and basket hooks that used
// to live here are in useCartQueries.js and take a retailer.

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
