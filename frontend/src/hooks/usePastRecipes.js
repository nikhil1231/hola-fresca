import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { fetchCooked, setCookedMark } from '../api/planClient.js'
import { PANTRY_KEY } from './usePantry.js'

// Which past recipes were cooked. Mostly assumed rather than asked for, so what
// travels here are the corrections.
//
// A correction is not only a fact about history: unticking a recipe puts its
// share of every lot back on the shelf, which changes what the next basket
// buys. Hence the pantry and the priced basket are invalidated alongside.

export const COOKED_KEY = ['cooked']

export function useCooked(weekStarts) {
  return useQuery({
    queryKey: [...COOKED_KEY, weekStarts],
    queryFn: () => fetchCooked(weekStarts),
    enabled: weekStarts.length > 0,
    placeholderData: (previous) => previous,
  })
}

export function useSetCooked() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: setCookedMark,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: COOKED_KEY })
      queryClient.invalidateQueries({ queryKey: PANTRY_KEY })
      queryClient.invalidateQueries({ queryKey: ['planner-basket'] })
    },
  })
}
