import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { fetchRetailers, setActiveRetailer } from '../api/retailerClient.js'

const RETAILERS_KEY = ['retailers']

// Which shop you are in changes what almost every other query answers — the
// browse prices, the basket, the mapping queue — so it is fetched once and held
// for the session rather than re-asked per page. Nothing but the toggle below
// can change it.
export function useRetailers() {
  return useQuery({
    queryKey: RETAILERS_KEY,
    queryFn: fetchRetailers,
    staleTime: Infinity,
  })
}

/** The active retailer, as `{ id, label, shoppable }`.
 *
 *  Returns nulls rather than a guessed default while the query is in flight, so
 *  a chip renders nothing instead of flashing the wrong shop's name — being
 *  briefly told you are shopping at Ocado when you are not is worse than a beat
 *  of blank space. */
export function useActiveRetailer() {
  const { data, isPending } = useRetailers()
  const active = data?.items?.find((item) => item.id === data.active) ?? null
  return {
    id: active?.id ?? null,
    label: active?.label ?? null,
    shoppable: active?.shoppable ?? false,
    isPending,
  }
}

export function useSetRetailer() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: setActiveRetailer,
    onSuccess: (data) => {
      queryClient.setQueryData(RETAILERS_KEY, data)
      // Every priced or mapped read was answered for the old shop. Rather than
      // enumerate them — and miss one — drop the lot and let the visible pages
      // re-ask. Switching shops is rare and deliberate, so the refetch is
      // affordable; showing Ocado's prices under a Sainsbury's chip is not.
      queryClient.invalidateQueries()
    },
  })
}
