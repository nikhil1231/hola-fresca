import { useMutation, useQuery } from '@tanstack/react-query'

import { fetchOcadoSlots, reserveOcadoSlot } from '../api/ocadoClient.js'

// Delivery slots, which only Ocado has. The session and basket hooks that used
// to live here are in useCartQueries.js and take a retailer.

export function useOcadoSlots(params, { enabled = true } = {}) {
  return useQuery({
    queryKey: ['ocado-slots', params],
    queryFn: () => fetchOcadoSlots(params),
    // Gated on the connection rather than on an account id, which the caller no
    // longer has: without a connected account this is a guaranteed 404.
    enabled,
  })
}

export function useOcadoReserve() {
  return useMutation({
    mutationFn: reserveOcadoSlot,
  })
}
